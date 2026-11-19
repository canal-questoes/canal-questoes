"""Motor falso estrito, com injeção de falhas nos testes.


Servidor HTTP de verdade (`ThreadingHTTPServer` em `127.0.0.1`, porta livre, HTTP/1.1 com
`Content-Length`, `Connection: keep-alive` e `Keep-Alive: timeout=5`, como o motor de questões), alcançado
pelo adaptador real (`HttpQuestionEngine`) pela URL `base_url`. Só usa a biblioteca padrão, o
`jsonschema` e o `referencing` (grupo `dev`) e **não** importa `cq_server`: se o adaptador errar, o
dublê continua capaz de acusar.

**Rotas** (as do motor, `engine/src/http/server.ts`): `GET /v1/health`, `POST /v1/inspect`,
`POST /v1/variants` e `POST /v1/grade`, com as operações `health`, `inspect`, `create_variant` e
`grade` (os nomes de `fail`, `slow`, `calls` etc.). Fora delas, 404 `route_not_found`; método errado
numa rota, 405 `method_not_allowed` com `Allow`; corpo acima de 2 MiB, 413 `payload_too_large`; corpo
que não é JSON, 400 `invalid_request`. As mensagens de erro são as do motor
(`engine/src/contract/error-response.ts`, `engine/src/core/reference.ts`).

**Estrito**: tudo o que desvia do contrato vai para `protocol_errors()`, que a
fixture `engine_fake` exige vazio:
- rota ou método fora das 4, consulta na URL, `Content-Type` diferente de `application/json` (UTF-8),
  JSON inválido ou com `NaN`/`Infinity`, corpo com `Transfer-Encoding`, corpo acima de 2 MiB (o
  adaptador tem de recusar antes de enviar) e seed fora de [0, 2³²);
- **todo pedido** validado contra `$defs.<op>_request` e **toda resposta que o próprio dublê vai
  enviar**, inclusive as injetadas por `fail` e `inject`, contra `$defs.<op>_response`,
  `health_response` ou `error_response` (a IR contra `ir.schema.json`, pelo `$ref`), com um
  `referencing.Registry` que resolve os dois `$id` e com o verificador de `format` ligado; um erro com
  status incoerente com o código (a tabela `ERROR_STATUS`) também é acusado. Só `inject(…,
  off_contract=True)`, `set_health` com versões de outro contrato e as falhas de rede de
  `fail_network` saem fora do contrato **declaradamente**;
- **resposta crua** do `grade`: o dublê aceita
  exatamente as formas que o `answers.raw_for_engine` produz, conferidas contra os campos da IR da
  variante: `single_choice` ausente (o "Enviar" sem marcação, como o PL recebe o formulário sem rádio
  marcado: `pl-multiple-choice.py@a193f72` responde "No answer was submitted.", ou nota 0 com
  `allow-blank`) ou `str`
  qualquer (a chave marcada pelo "Enviar" ou o texto digitado literal); presente com outro tipo
  (inclusive `null`) é recusada;
  `multiple_choice` ausente (seleção vazia pelos botões), `str` qualquer (uma marcação pelo botão ou o
  texto digitado literal, inclusive `""`, `","`, `"b,a"` e `"ab"`, pela adaptação de canal da escolha múltipla
  digitada) ou lista de 2 ou mais chaves distintas na ordem das opções (a seleção
  pelos botões); a lista com algum token que não é chave (`["a","z"]`,
  `["ab"]`) é recusada; `number`, `integer` e `text` presentes e `str`; nenhum campo fora da IR. O
  resto é erro de protocolo, salvo `expect_raw_shape(False)`. Na correção de uma `str` da múltipla, o
  dublê segue o `pl-checkbox.py@a193f72:726-752`: um elemento por caractere (`set(submitted_key)`; `"ab"`
  vale {a, b}), a chave inválida é o primeiro caractere que não é chave (`"b,a"` → `,`; `"a b"` → o
  espaço) e o intervalo de marcações conta `len(submitted_key)` (`"aa"` conta 2);
- **snapshot**: o do `grade` tem de ser igual (JSON) a um snapshot emitido pelo dublê para a mesma
  referência, salvo `accept_foreign_snapshots()`;
- **determinismo**: o dublê guarda a resposta de cada `(op, pedido)` e acusa se a mesma entrada der
  outra saída (as respostas programadas ficam fora);
- **questão no disco sem programação** (`program`): erro de protocolo e 500 `internal_error`.

**Existência em disco** (`engine/src/core/reference.ts`): com `repos_root`, a questão só
existe se houver `<repos_root>/<sha256(repo)>/<commit>/questions/<qid>/info.json`; sem isso, 404
`not_found` com a mensagem do motor (repositório, commit, questão ou `info.json` ausente), que **não**
é desvio. Sem `repos_root`, toda referência dá 404. A obtenção falsa (`tests/helpers/repos.py`)
escreve na mesma raiz; `delete_tree` exercita a nova obtenção de ponta a ponta.

**Presets** (`preset(nome)`, `PRESETS`; um QID de mesmo nome em `tests/fixtures/repos/course/`): os 5
tipos de campo (`single_choice`, `multiple_choice`, `number`, `integer`, `text`), `multipart` (os
5), `unsupported` (estática), `dynamic_unsupported` (na criação da variante), `broken_on_variant`,
`broken_on_grade` (ambos com `technical_detail`, o controle positivo do descarte no adaptador),
`with_media`, `missing_media`, `media_error`, `server_format_error` (erro fora de `fields[]`),
`score_above_one` (nota 1,5 na resposta certa), `version_mismatch` (as duas marcas na correção),
`single_variant`, `no_show_correct_answer` e `canary` (título, enunciado, rótulo, dica e aviso com o
texto de identidade que o teste passa). Os erros de formato são **os textos reais do PL**
`a193f72` (`engine/test/integration/grade.test.ts`), em `html` e `text`.

**Gabarito sentinela**: o `correct_answers` de todo snapshot e de toda
correção leva a chave `gabarito_sentinela` com `answer_sentinel` (padrão `ANSWER_SENTINEL`,
configurável por `reset(answer_sentinel=…)`), e todo `answer_key[].display_html` e toda explicação
terminam com `<code>{answer_sentinel}</code>`. O enunciado, os rótulos, as dicas e as opções nunca o
contêm.

**Falhas injetáveis:** `fail(op, error=…, times=…, when=…)` (código do contrato ou status),
`inject(op, status=…, body=…)`, `fail_network(op, kind=…)` (`NETWORK_KINDS`), `slow(op, seconds,
drip=…)`, `set_health(...)`, `set_versions(...)`, `inject_version_mismatch(...)`, `stop()`/`start()`
na mesma porta (conexão recusada), `release_hangs()` e `clear_failures()`.

**Observação:** `calls(op)` (`EngineCall`: corpo do pedido, status, corpo da resposta,
`started`/`ended` de `monotonic()`, conexão), `connection_count()`, `issued_variants()`,
`wait_for`, `protocol_errors()`.

**Silencioso:** nada em stdout nem stderr (`log_message`, `log_error` e `handle_error`
sobrescritos; `OSError` na escrita engolido). Os corpos, com as respostas dos alunos, ficam só na
memória do processo de teste.
"""

from __future__ import annotations

import atexit
import base64
import copy
import hashlib
import html as html_lib
import http.client
import itertools
import json
import os
import pathlib
import re
import select
import socket
import socketserver
import struct
import threading
import time
import urllib.parse
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypeVar

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

T = TypeVar("T")

# ---------------------------------------------------------------------------------------------
# Contrato do motor (contracts/engine/v1; engine/src/http/server.ts; engine/src/contract)
# ---------------------------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parents[2]
ENGINE_CONTRACTS = REPO / "contracts" / "engine" / "v1"
API_SCHEMA_ID = "https://canal-questoes/contracts/engine/v1/engine-api.schema.json"
IR_SCHEMA_ID = "https://canal-questoes/contracts/engine/v1/ir.schema.json"

ENGINE_VERSION = "1.0.0"
PL_COMMIT = "a193f72048530a4499252c454acca963cdf98922"
CONTRACT_VERSION = "1.0"
IR_VERSION = "1.0"
WORKERS = 4
MAX_BODY_BYTES = 2 * 1024 * 1024  # engine/src/http/server.ts, MAX_BODY_BYTES
MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # o teto do adaptador
KEEPALIVE_SECONDS = 5.0  # o motor fecha a conexão ociosa em 5 s: `Keep-Alive: timeout=5`
SEED_LIMIT = 2**32
SOCKET_CHECK_SECONDS = 0.01

OPS: tuple[str, ...] = ("health", "inspect", "create_variant", "grade")
ROUTES: dict[str, tuple[str, str]] = {
    "/v1/health": ("health", "GET"),
    "/v1/inspect": ("inspect", "POST"),
    "/v1/variants": ("create_variant", "POST"),
    "/v1/grade": ("grade", "POST"),
}
PATH_OF: dict[str, str] = {op: path for path, (op, _method) in ROUTES.items()}
REQUEST_DEF: dict[str, str] = {"inspect": "inspect_request", "create_variant": "create_variant_request",
                               "grade": "grade_request"}
RESPONSE_DEF: dict[str, str] = {"health": "health_response", "inspect": "inspect_response",
                                "create_variant": "create_variant_response", "grade": "grade_response"}

# engine/src/contract/error-response.ts
ERROR_STATUS: dict[str, int] = {
    "invalid_request": 400,
    "not_found": 404,
    "payload_too_large": 413,
    "route_not_found": 404,
    "method_not_allowed": 405,
    "internal_error": 500,
}
ERROR_MESSAGES: dict[str, str] = {
    "invalid_request": "Pedido inválido: o corpo não segue o contrato da API do motor de questões.",
    "not_found": ("Questão não encontrada: o repositório, o commit ou o QID não existe na área de "
                  "repositórios do motor."),
    "payload_too_large": "O corpo do pedido excede o tamanho máximo de 2 MiB.",
    "route_not_found": ("Rota desconhecida: as rotas da API são GET /v1/health e POST /v1/inspect, "
                        "/v1/variants e /v1/grade."),
    "method_not_allowed": ("Método não permitido nesta rota: /v1/health aceita só GET, e /v1/inspect, "
                           "/v1/variants e /v1/grade, só POST."),
    "internal_error": "Falha interna do motor de questões; o pedido pode ser repetido.",
}
# `fail(op, error=<status>)`: o status leva ao código (404 é o `not_found` da referência).
ERROR_OF_STATUS: dict[int, str] = {400: "invalid_request", 404: "not_found", 405: "method_not_allowed",
                                   413: "payload_too_large", 500: "internal_error"}
INVALID_JSON_MESSAGE = "Pedido inválido: o corpo não é JSON válido."
INVALID_JSON_DETAIL = "(corpo): o texto recebido não pôde ser interpretado como JSON."
# engine/src/core/reference.ts
SEED_TOO_LARGE_MESSAGE = "Seed inválida: o valor em base 36 precisa ser menor que 2^32 (4294967296)."
NOT_FOUND_REPOSITORY = "Repositório não encontrado na área de repositórios do motor."
NOT_FOUND_COMMIT = "Commit não encontrado para este repositório na área de repositórios do motor."
NOT_FOUND_QUESTION = "Questão não encontrada no curso."
NOT_FOUND_INFO_JSON = "Questão não encontrada no curso: falta o arquivo info.json."

# Falhas de rede de `fail_network`.
NETWORK_KINDS: tuple[str, ...] = ("reset", "hang", "not_json", "html_502", "truncated", "oversize",
                                  "oversize_chunked", "redirect", "nan")
HTML_BAD_GATEWAY = (b"<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n"
                    b"<center><h1>502 Bad Gateway</h1></center>\r\n<hr><center>nginx</center>\r\n"
                    b"</body>\r\n</html>\r\n")

FIELD_TYPES: tuple[str, ...] = ("number", "integer", "text", "single_choice", "multiple_choice")
ELEMENT_OF: dict[str, str] = {"number": "pl-number-input", "integer": "pl-integer-input",
                              "text": "pl-string-input", "single_choice": "pl-multiple-choice",
                              "multiple_choice": "pl-checkbox"}

# Sentinelas: o gabarito e o detalhe técnico que o adaptador descarta.
ANSWER_SENTINEL = "GABARITO-SENTINELA-7F3A"
SENTINEL_KEY = "gabarito_sentinela"
TECHNICAL_DETAIL_SENTINEL = "cq-detalhe-tecnico-sentinela-5B1D"
TECHNICAL_DETAIL = ("Traceback (most recent call last):\n  File \"server.py\", line 2, in grade\n"
                    f"ValueError: {TECHNICAL_DETAIL_SENTINEL}")


def monotonic() -> float:
    """O relógio de `started`/`ended`: `time.perf_counter()` (monotônico e de alta resolução; o
    `time.monotonic()` anda em passos de 15,6 ms no Windows)."""
    return time.perf_counter()


def seed_value(seed: str) -> int:
    """O valor da seed como o motor a lê (`parseInt(seed, 36)`)."""
    return int(seed, 36)


# ---------------------------------------------------------------------------------------------
# Textos reais do PL a193f72 (engine/test/integration/grade.test.ts;
# engine/conformance/compare.test.ts)
# ---------------------------------------------------------------------------------------------

NUMBER_INVALID_HTML = (
    "<p>\n            Invalid format: The submitted answer could not be interpreted as a decimal number.\n"
    "        Do not write variables or other symbols.\n    </p>\n    <p>Example <i>valid</i> inputs: "
    "<code>5</code><code class=\"ms-3\">-17</code><code class=\"ms-3\">4.25e7</code><code class=\"ms-3\">"
    "3/7</code></p>\n    <p>Example <i>invalid</i> inputs: <code>6+9</code><code class=\"ms-3\">4*10^7"
    "</code><code class=\"ms-3\">3*pi</code><code class=\"ms-3\">3.6i</code></p>")
NUMBER_INVALID_TEXT = (
    "Invalid format: The submitted answer could not be interpreted as a decimal number. Do not write "
    "variables or other symbols. Example valid inputs: 5-174.25e73/7 Example invalid inputs: "
    "6+94*10^73*pi3.6i")
INTEGER_INVALID_HTML = (
    "<p> Invalid format:\n            the submitted answer could not be interpreted as an integer.  No "
    "symbolic input, fractions, or scientific notation are allowed.\n    </p>\n    <p> Example <i>valid"
    "</i> inputs: <code class=\"me-2\">1</code> <code class=\"me-2\">-100</code> </p>\n    <p> Example "
    "<i>invalid</i> inputs: <code class=\"me-2\">6+9</code> <code class=\"me-2\">3.7</code> <code "
    "class=\"me-2\">3*pi</code> <code class=\"me-2\">6e19</code> <code class=\"me-2\">0x1a</code> <code "
    "class=\"me-2\">-0b0110</code> </p>\n    <hr>\n    <small>\n        <p> The submitted answer must be "
    "formatted as an integer. </p>\n    </small>")
INTEGER_INVALID_TEXT = (
    "Invalid format: the submitted answer could not be interpreted as an integer. No symbolic input, "
    "fractions, or scientific notation are allowed. Example valid inputs: 1 -100 Example invalid inputs: "
    "6+9 3.7 3*pi 6e19 0x1a -0b0110 The submitted answer must be formatted as an integer.")
NO_SUBMITTED_ANSWER = "No submitted answer."
NUMBER_BLANK = "The submitted answer was blank."
NUMBER_DIVISION_BY_ZERO = "Your expression resulted in a division by zero."
INTEGER_BLANK = "Invalid format: the submitted answer was blank."
TEXT_BLANK = "Invalid format. The submitted answer was left blank."
SINGLE_NO_ANSWER = "No answer was submitted."


def html_to_text(html: str) -> str:
    """O `text` de um erro de formato (o do motor de questões): tags fora, entidades decodificadas,
    espaços consecutivos colapsados num só e aparado."""
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]*>", "", html))).strip()


def _format_error(html: str, text: str | None = None) -> dict[str, str]:
    return {"html": html, "text": html_to_text(html) if text is None else text}


def invalid_choice_error(key: str) -> dict[str, str]:
    """`pl-multiple-choice.py`: `Invalid choice: <code …>{escape(key)}</code>` (ecoa a escolha)."""
    return _format_error(f'Invalid choice: <code class="user-output-invalid">{html_lib.escape(key)}</code>')


def invalid_option_error(key: str) -> dict[str, str]:
    """`pl-checkbox.py:736-740`: `You selected an invalid option: {escape(key)}` (ecoa a marcação)."""
    return _format_error(f"You selected an invalid option: {html_lib.escape(key)}")


def select_count_error(min_select: int, max_select: int) -> dict[str, str]:
    """`pl-checkbox.py` (`generate_options_range_text`): entre `min` e `max`, ou exatamente `n`."""
    if min_select == max_select:
        noun = "option" if min_select == 1 else "options"
        return _format_error(f"You must select exactly <b>{min_select}</b> {noun}.")
    return _format_error(f"You must select between <b>{min_select}</b> and <b>{max_select}</b> options.")


def integer_invalid_error(base: int) -> dict[str, str]:
    if base == 10:
        return {"html": INTEGER_INVALID_HTML, "text": INTEGER_INVALID_TEXT}
    suffix = f"as an integer in base {base}."
    return {"html": INTEGER_INVALID_HTML.replace("as an integer.", suffix, 1),
            "text": INTEGER_INVALID_TEXT.replace("as an integer.", suffix, 1)}


# ---------------------------------------------------------------------------------------------
# Validação contra os esquemas do motor de questões (`format` ligado; 100% das chamadas)
# ---------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _registry() -> Registry:
    resources = []
    for name in ("engine-api.schema.json", "ir.schema.json"):
        contents = json.loads((ENGINE_CONTRACTS / name).read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def _validator(definition: str) -> Draft202012Validator:
    target = IR_SCHEMA_ID if definition == "ir" else f"{API_SCHEMA_ID}#/$defs/{definition}"
    return Draft202012Validator({"$ref": target}, registry=_registry(),
                                format_checker=Draft202012Validator.FORMAT_CHECKER)


def schema_errors(definition: str, instance: Any) -> list[str]:
    """Os desvios de `instance` contra `$defs.<definition>` do `engine-api.schema.json` (ou contra o
    `ir.schema.json` com `definition="ir"`), cada um com o caminho e a mensagem (truncada)."""
    found = []
    for error in _validator(definition).iter_errors(instance):
        where = "/".join(str(part) for part in error.absolute_path) or "(raiz)"
        message = error.message if len(error.message) <= 200 else error.message[:200] + "…"
        found.append(f"{where}: {message}")
    return sorted(found)


def canonical(value: Any) -> str:
    """JSON canônico (chaves ordenadas, sem espaços) para comparar snapshots e respostas."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def repo_key(repo: str) -> str:
    """sha256 hex de `utf8(repo)`: o diretório do repositório na área (o `repoKey` do motor)."""
    return hashlib.sha256(repo.encode("utf-8")).hexdigest()


def question_content_hash(question_dir: pathlib.Path) -> str:
    """`sha256:` + o hash do diretório da questão (o do motor de questões): por arquivo, em ordem do
    caminho relativo, `caminho \\0 tamanho \\0 bytes \\0`; links e diretórios vazios fora."""
    digest = hashlib.sha256()
    files = sorted((p for p in question_dir.rglob("*") if p.is_file() and not p.is_symlink()),
                   key=lambda p: p.relative_to(question_dir).as_posix())
    for path in files:
        data = path.read_bytes()
        digest.update(path.relative_to(question_dir).as_posix().encode("utf-8") + b"\0")
        digest.update(str(len(data)).encode("ascii") + b"\0" + data + b"\0")
    return "sha256:" + digest.hexdigest()


def png_bytes(size: int | None = None) -> bytes:
    """Um PNG 1×1 válido; com `size`, completado por um bloco auxiliar até ter exatamente `size`
    bytes (para os limites de foto e documento)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    head = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    tail = chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b"")
    if size is None:
        return head + tail
    padding = size - len(head) - len(tail) - 12
    if padding < 0:
        raise ValueError(f"png_bytes: o menor PNG tem {len(head) + len(tail)} bytes")
    return head + chunk(b"cqPd", b"\0" * padding) + tail


# ---------------------------------------------------------------------------------------------
# Questões programáveis e presets
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeOption:
    key: str
    html: str


@dataclass(frozen=True)
class FakeField:
    """Um campo de resposta da questão falsa (os 5 tipos da IR).

    `correct` é o valor de `correct_answers[name]`: `float` (number), `int` (integer), `str`
    (text; chave em single_choice) ou lista de chaves na ordem das opções (multiple_choice). `label` e
    `suffix` são `label_html`/`suffix_html`; `hint` sem valor usa a dica do motor para o tipo.
    """

    name: str
    type: str
    correct: Any
    label: str | None = None
    suffix: str | None = None
    hint: str | None = None
    options: tuple[FakeOption, ...] = ()
    weight: int = 1
    allow_blank: bool = False
    base: int = 10
    ignore_case: bool = False
    min_select: int | None = None
    max_select: int | None = None
    partial_credit: str = "net-correct"
    can_show: bool = True
    display: str | None = None


@dataclass(frozen=True)
class FakeMedia:
    """Uma mídia do enunciado (`<img src="cq-media:mN">`); `data` sem valor usa `png_bytes()`."""

    filename: str
    content_type: str = "image/png"
    alt: str | None = "Figura"
    status: str = "ok"
    origin: str = "static"
    data: bytes | None = None


@dataclass(frozen=True)
class FakeReason:
    code: str
    message: str
    elements: tuple[str, ...] = ()


@dataclass(frozen=True)
class FakeBroken:
    code: str
    message: str
    phase: str
    technical_detail: str | None = TECHNICAL_DETAIL


@dataclass(frozen=True)
class FakeWarning:
    code: str
    message: str
    phase: str | None = None


@dataclass(frozen=True)
class FakeFormatError:
    """Erro de formato acrescentado pelo `grade()` do `server.py` (chave fora de `fields[]`)."""

    key: str
    html: str


@dataclass(frozen=True)
class FakeQuestion:
    """O comportamento programado de uma questão (`FakeEngine.program`).

    - `inspect_reasons` não vazio: "não suportada" estática (inspeção e criação da variante);
    - `variant_reasons`: "não suportada" só na criação da variante (checagem dinâmica do motor de questões);
    - `variant_broken`/`grade_broken`: `broken` na criação da variante ou na correção;
    - `server_format_error`: erro de formato fora de `fields[]` numa resposta sem outro erro;
    - `score_override`: a nota que o `grade()` do `server.py` põe quando a calculada é ≥ 1;
    - `version_mismatch`: as marcas `(engine, pl)` fixas na correção (senão, comparadas com o snapshot);
    - `server_score`: a nota que o `grade()` do `server.py` põe sempre, numa resposta sem erro de formato
      (depois do `score_override`; vale abaixo de 1, negativa ou acima de 1; as notas das partes ficam como
      calculadas). Para o formato do feedback de nota parcial.
    """

    title: str
    statement: str
    fields: tuple[FakeField, ...]
    uuid: str | None = None
    topic: str = "Teste"
    tags: tuple[str, ...] = ("teste",)
    media: tuple[FakeMedia, ...] = ()
    explanation: str | None = None
    show_correct_answer: bool = True
    single_variant: bool = False
    partial_credit: bool = True
    extra_elements: tuple[str, ...] = ()
    inspect_reasons: tuple[FakeReason, ...] = ()
    variant_reasons: tuple[FakeReason, ...] = ()
    variant_broken: FakeBroken | None = None
    grade_broken: FakeBroken | None = None
    variant_warnings: tuple[FakeWarning, ...] = ()
    grade_warnings: tuple[FakeWarning, ...] = ()
    server_format_error: FakeFormatError | None = None
    score_override: float | None = None
    version_mismatch: tuple[bool, bool] | None = None
    server_score: float | None = None


COLOR_OPTIONS = (FakeOption("a", "Vermelho"), FakeOption("b", "Azul"), FakeOption("c", "Verde"),
                 FakeOption("d", "Amarelo"))
PRIME_OPTIONS = (FakeOption("a", "Dois"), FakeOption("b", "Nove"), FakeOption("c", "Sete"),
                 FakeOption("d", "Dez"))


def number_field(name: str = "resposta", *, correct: float = 6.5, **kw: Any) -> FakeField:
    return FakeField(name, "number", correct, **{"label": "Velocidade:", "suffix": "m/s", **kw})


def integer_field(name: str = "resposta", *, correct: int = 42, **kw: Any) -> FakeField:
    return FakeField(name, "integer", correct, **{"label": "Quantidade:", **kw})


def text_field(name: str = "resposta", *, correct: str = "azul", **kw: Any) -> FakeField:
    return FakeField(name, "text", correct, **{"label": "Cor:", **kw})


def single_choice_field(name: str = "resposta", *, correct: str = "b", **kw: Any) -> FakeField:
    return FakeField(name, "single_choice", correct, **{"options": COLOR_OPTIONS, **kw})


def multiple_choice_field(name: str = "resposta", *, correct: tuple[str, ...] | list[str] = ("a", "c"),
                          **kw: Any) -> FakeField:
    return FakeField(name, "multiple_choice", list(correct), **{"options": PRIME_OPTIONS, **kw})


PRESETS: tuple[str, ...] = (
    "single_choice", "multiple_choice", "number", "integer", "text", "multipart", "unsupported",
    "dynamic_unsupported", "broken_on_variant", "broken_on_grade", "with_media", "missing_media",
    "media_error", "server_format_error", "score_above_one", "version_mismatch", "single_variant",
    "no_show_correct_answer", "canary",
)
# `program_presets()` programa todos menos o canário (que exige o texto de identidade).
IDENTITY_FREE_PRESETS: tuple[str, ...] = tuple(p for p in PRESETS if p != "canary")

UNSUPPORTED_REASON = FakeReason(
    "unsupported_elements", "A questão usa elementos que o canal não suporta: pl-matrix-input.",
    ("pl-matrix-input",))
DYNAMIC_UNSUPPORTED_REASON = FakeReason(
    "unsupported_elements", "A variante gerada usa elementos que o canal não suporta: pl-matrix-input.",
    ("pl-matrix-input",))
VARIANT_BROKEN = FakeBroken("question_code_error", "O código da questão falhou ao gerar a variante.",
                            "generate")
GRADE_BROKEN = FakeBroken("question_code_error", "O código da questão falhou ao corrigir a resposta.",
                          "grade")
MISSING_MEDIA_WARNING = FakeWarning("missing_media", "Uma imagem do enunciado não foi encontrada.",
                                    "render")
MEDIA_ERROR_WARNING = FakeWarning("media_error", "A geração de uma imagem do enunciado falhou.", "file")
SERVER_FORMAT_ERROR = FakeFormatError("verificacao", "The answer failed the server check.")


def _preset_uuid(name: str) -> str:
    return f"5f0c7a52-2d1e-4b7a-9c3e-{PRESETS.index(name) + 1:012d}"


def preset(name: str, **overrides: Any) -> FakeQuestion:
    """A questão do preset `name` (`PRESETS`), com `overrides` sobre os campos de `FakeQuestion`.

    O `canary` exige `identity` (o texto de identidade, ex.: nome, RA e e-mail do aluno canário) e
    aceita `hint` (≤ 64 caracteres; padrão: o começo de `identity`) e `warning` (padrão: `identity`);
    o texto sai no título, no enunciado, no rótulo, na dica e num aviso `console_output`.
    """
    if name not in PRESETS:
        raise ValueError(f"preset desconhecido: {name}")
    uuid = _preset_uuid(name)
    q: FakeQuestion
    if name == "single_choice":
        q = FakeQuestion("Cor do céu", "Qual é a cor do céu num dia limpo?", (single_choice_field(),),
                         explanation="O céu limpo é azul pelo espalhamento de Rayleigh.")
    elif name == "multiple_choice":
        q = FakeQuestion("Números primos", "Marque os números primos.", (multiple_choice_field(),))
    elif name == "number":
        q = FakeQuestion("Velocidade média", "Um corpo percorre 13 m em 2 s. Qual é a velocidade média?",
                         (number_field(),))
    elif name == "integer":
        q = FakeQuestion("Contagem", "Quantas unidades há em quatro dezenas e duas unidades?",
                         (integer_field(),))
    elif name == "text":
        q = FakeQuestion("Cor em uma palavra",
                         "Escreva, em uma palavra minúscula, a cor do céu num dia limpo.", (text_field(),))
    elif name == "multipart":
        q = FakeQuestion("Várias partes", "Responda às cinco partes.",
                         (number_field("num"), integer_field("int", correct=31), text_field("txt"),
                          single_choice_field("mc"), multiple_choice_field("cb")),
                         explanation="Cada parte vale o mesmo peso.")
    elif name == "unsupported":
        q = FakeQuestion("Matriz", "Preencha a matriz.", (number_field(),),
                         extra_elements=("pl-matrix-input",), inspect_reasons=(UNSUPPORTED_REASON,))
    elif name == "dynamic_unsupported":
        q = FakeQuestion("Elemento injetado", "A variante injeta um elemento fora do subconjunto.",
                         (number_field(),), variant_reasons=(DYNAMIC_UNSUPPORTED_REASON,))
    elif name == "broken_on_variant":
        q = FakeQuestion("Quebrada na variante", "O generate desta questão falha.", (number_field(),),
                         variant_broken=VARIANT_BROKEN)
    elif name == "broken_on_grade":
        q = FakeQuestion("Quebrada na correção", "O grade desta questão falha.", (number_field(),),
                         grade_broken=GRADE_BROKEN)
    elif name == "with_media":
        q = FakeQuestion("Com figura", "Observe a figura.", (number_field(),),
                         media=(FakeMedia("clientFilesQuestion/figura.png"),),
                         extra_elements=("pl-figure",))
    elif name == "missing_media":
        q = FakeQuestion("Figura ausente", "A figura desta questão não existe.", (number_field(),),
                         media=(FakeMedia("clientFilesQuestion/ausente.png", status="missing"),),
                         extra_elements=("pl-figure",), variant_warnings=(MISSING_MEDIA_WARNING,))
    elif name == "media_error":
        q = FakeQuestion("Figura gerada com erro", "A figura gerada desta questão falha.", (number_field(),),
                         media=(FakeMedia("grafico.png", status="missing", origin="dynamic"),),
                         extra_elements=("pl-figure",), variant_warnings=(MEDIA_ERROR_WARNING,))
    elif name == "server_format_error":
        q = FakeQuestion("Erro do server.py", "O grade do server.py acrescenta um erro de formato.",
                         (number_field(),), server_format_error=SERVER_FORMAT_ERROR)
    elif name == "score_above_one":
        q = FakeQuestion("Nota acima de 1", "O grade do server.py dá nota 1,5 à resposta certa.",
                         (number_field(),), score_override=1.5)
    elif name == "version_mismatch":
        q = FakeQuestion("Versões diferentes", "A correção acusa versões diferentes do motor e do PL.",
                         (number_field(),), version_mismatch=(True, True))
    elif name == "single_variant":
        q = FakeQuestion("Variante única", "Questão de variante única.", (single_choice_field(),),
                         single_variant=True, explanation="A resposta é a cor do céu.")
    elif name == "no_show_correct_answer":
        q = FakeQuestion("Sem gabarito", "Questão que não mostra o gabarito.", (single_choice_field(),),
                         show_correct_answer=False, explanation="Explicação que não pode ser mostrada.")
    else:  # canary
        identity = overrides.pop("identity", None)
        if not isinstance(identity, str) or not identity:
            raise ValueError("preset('canary') exige identity=<texto de identidade>")
        hint = overrides.pop("hint", None) or identity[:64]
        warning = overrides.pop("warning", None) or identity
        escaped = html_lib.escape(identity, quote=False)
        q = FakeQuestion(f"Questão de {identity}", f"Responda, {escaped}: qual é a velocidade média?",
                         (number_field(label=f"{escaped}:", hint=hint),),
                         variant_warnings=(FakeWarning("console_output", warning, "generate"),))
    q = replace(q, uuid=uuid)
    return replace(q, **overrides) if overrides else q


# ---------------------------------------------------------------------------------------------
# Construção da variante e correção (espelham o PL)
# ---------------------------------------------------------------------------------------------


def _hint(f: FakeField) -> str:
    """A dica do motor para o tipo (`engine/src/ir/fields.ts`), com os padrões dos elementos."""
    if f.hint is not None:
        return f.hint
    if f.type == "number":
        return "Número; ex.: 4.25, -17 ou 3/7"
    if f.type == "integer":
        if f.base == 10:
            return "Número inteiro; ex.: 42 ou -17"
        return (f"Número inteiro na base {f.base}; ex.: {_base_repr(42, f.base)} ou "
                f"-{_base_repr(17, f.base)}")
    if f.type == "text":
        return "Texto; não diferencia maiúsculas" if f.ignore_case else "Texto"
    if f.type == "single_choice":
        return "Escolha uma alternativa"
    low, high = _select_bounds(f)
    if low == high:
        return f"Marque exatamente {low} {'alternativa' if low == 1 else 'alternativas'}"
    return f"Marque de {low} a {high} alternativas"


def _base_repr(value: int, base: int) -> str:
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = ""
    while value:
        value, rest = divmod(value, base)
        out = digits[rest] + out
    return out or "0"


def _select_bounds(f: FakeField) -> tuple[int, int]:
    low = 1 if f.min_select is None else f.min_select
    high = len(f.options) if f.max_select is None else f.max_select
    return low, high


def _content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()


def _display(f: FakeField) -> str:
    if f.display is not None:
        return f.display
    by_key = {o.key: o.html for o in f.options}
    if f.type == "single_choice":
        return by_key.get(f.correct, str(f.correct))
    if f.type == "multiple_choice":
        return ", ".join(by_key.get(k, str(k)) for k in f.correct)
    if f.type == "number":
        return repr(float(f.correct)) if not float(f.correct).is_integer() else str(int(f.correct))
    return html_lib.escape(str(f.correct), quote=False)


def _ir_field(f: FakeField) -> dict[str, Any]:
    if f.type not in FIELD_TYPES:
        raise ValueError(f"tipo de campo desconhecido: {f.type}")
    constraints: dict[str, Any] = {"allow_blank": f.allow_blank}
    options: list[dict[str, Any]] | None = None
    fmt: dict[str, Any]
    if f.type == "number":
        fmt = {"kind": "number", "comparison": "relabs", "rtol": 0.01, "atol": 1e-08,
               "allow_fractions": True, "allow_complex": False}
    elif f.type == "integer":
        fmt = {"kind": "integer", "base": f.base}
    elif f.type == "text":
        fmt = {"kind": "text", "ignore_case": f.ignore_case, "remove_spaces": False,
               "remove_leading_trailing": False, "normalize_to_ascii": False, "multiline": False,
               "regex": False}
    elif f.type == "single_choice":
        fmt = {"kind": "single_choice"}
    else:
        fmt = {"kind": "multiple_choice", "partial_credit": f.partial_credit}
        low, high = _select_bounds(f)
        constraints = {"allow_blank": f.allow_blank, "min_select": low, "max_select": high}
    if f.type in ("single_choice", "multiple_choice"):
        options = [{"key": o.key, "html": o.html, "content_hash": _content_hash(o.html)} for o in f.options]
    return {"name": f.name, "type": f.type, "label_html": f.label, "suffix_html": f.suffix,
            "format": fmt, "hint": _hint(f), "constraints": constraints, "weight": f.weight,
            "options": options}


def build_variant(q: FakeQuestion, seed: str, *, sentinel: str = ANSWER_SENTINEL,
                  engine_version: str = ENGINE_VERSION, pl_commit: str = PL_COMMIT,
                  ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """(snapshot, IR, avisos) da variante `seed` de `q`, deterministicamente: a seed entra em
    `params.variante`; o enunciado, os campos e o gabarito são fixos."""
    params: dict[str, Any] = {"variante": seed_value(seed)}
    correct_answers: dict[str, Any] = {}
    for f in q.fields:
        if f.type in ("single_choice", "multiple_choice"):
            params[f.name] = [{"key": o.key, "html": o.html} for o in f.options]
        correct_answers[f.name] = copy.deepcopy(f.correct)
    correct_answers[SENTINEL_KEY] = sentinel
    snapshot = {"seed": seed, "params": params, "correct_answers": correct_answers, "preferences": {},
                "engine_version": engine_version, "pl_commit": pl_commit, "ir_version": IR_VERSION}
    media: list[dict[str, Any]] = []
    images = ""
    for index, m in enumerate(q.media, start=1):
        entry: dict[str, Any] = {"id": f"m{index}", "origin": m.origin, "filename": m.filename,
                                 "content_type": m.content_type, "alt": m.alt, "status": m.status}
        if m.status == "ok":
            data = png_bytes() if m.data is None else m.data
            entry["data_base64"] = base64.b64encode(data).decode("ascii")
            entry["size_bytes"] = len(data)
        media.append(entry)
        alt = "" if m.alt is None else f' alt="{html_lib.escape(m.alt)}"'
        images += f'<p><img src="cq-media:m{index}"{alt}></p>'
    markers = "".join(f'<p><cq-field name="{f.name}"></cq-field></p>' for f in q.fields)
    code = f"<code>{html_lib.escape(sentinel, quote=False)}</code>"
    ir: dict[str, Any] = {
        "ir_version": IR_VERSION,
        "statement": {"html": f"<p>{q.statement}</p>{images}{markers}"},
        "fields": [_ir_field(f) for f in q.fields],
        "answer_key": [{"name": f.name, "can_show": f.can_show and q.show_correct_answer,
                        "correct_value": copy.deepcopy(f.correct),
                        "display_html": f"{_display(f)} {code}"} for f in q.fields],
        "explanation": (None if q.explanation is None else
                        {"html": f"<p>{q.explanation} {code}</p>", "can_show": q.show_correct_answer}),
        "media": media,
    }
    warnings = [_warning(w) for w in q.variant_warnings]
    return snapshot, ir, warnings


def _warning(w: FakeWarning) -> dict[str, Any]:
    out: dict[str, Any] = {"code": w.code, "message": w.message}
    if w.phase is not None:
        out["phase"] = w.phase
    return out


def _reason(r: FakeReason) -> dict[str, Any]:
    out: dict[str, Any] = {"code": r.code, "message": r.message}
    if r.elements:
        out["elements"] = list(r.elements)
    return out


def _broken(b: FakeBroken) -> dict[str, Any]:
    out: dict[str, Any] = {"code": b.code, "message": b.message, "phase": b.phase}
    if b.technical_detail is not None:
        out["technical_detail"] = b.technical_detail
    return out


_DECIMAL = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_NUMBER_RE = re.compile(rf"\s*({_DECIMAL})\s*")
_FRACTION_RE = re.compile(rf"\s*({_DECIMAL})\s*/\s*({_DECIMAL})\s*")
_INTEGER_RE = re.compile(r"\s*[+-]?[0-9A-Za-z]+\s*")
_MISSING = object()


def _parse_field(f: FakeField, value: Any, correct: Any) -> tuple[Any, dict[str, str] | None, float | None]:
    """(resposta interpretada ou `_MISSING`, erro de formato ou `None`, nota da parte)."""
    if f.type == "number":
        if value is None:
            return _MISSING, _format_error(NO_SUBMITTED_ANSWER), None
        text = str(value)
        if text.strip() == "":
            if f.allow_blank:
                return None, None, 0.0
            return text, _format_error(NUMBER_BLANK), None
        match = _NUMBER_RE.fullmatch(text)
        if match:
            number = float(match.group(1))
        else:
            fraction = _FRACTION_RE.fullmatch(text)
            if fraction is None:
                return text, {"html": NUMBER_INVALID_HTML, "text": NUMBER_INVALID_TEXT}, None
            denominator = float(fraction.group(2))
            if denominator == 0:
                return text, _format_error(NUMBER_DIVISION_BY_ZERO), None
            number = float(fraction.group(1)) / denominator
        target = float(correct)
        ok = abs(number - target) <= 1e-08 + 0.01 * abs(target)
        return number, None, 1.0 if ok else 0.0
    if f.type == "integer":
        if value is None:
            return _MISSING, _format_error(NO_SUBMITTED_ANSWER), None
        text = str(value)
        if text.strip() == "":
            if f.allow_blank:
                return None, None, 0.0
            return text, _format_error(INTEGER_BLANK), None
        try:
            if not _INTEGER_RE.fullmatch(text):
                raise ValueError
            number_i = int(text.strip(), f.base)
        except ValueError:
            return text, integer_invalid_error(f.base), None
        return number_i, None, 1.0 if number_i == int(correct) else 0.0
    if f.type == "text":
        if value is None:
            return _MISSING, _format_error(NO_SUBMITTED_ANSWER), None
        text = str(value)
        if text == "":
            if f.allow_blank:
                return text, None, 0.0
            return text, _format_error(TEXT_BLANK), None
        ok = text.lower() == str(correct).lower() if f.ignore_case else text == str(correct)
        return text, None, 1.0 if ok else 0.0
    keys = [o.key for o in f.options]
    if f.type == "single_choice":
        if value is None:
            if f.allow_blank:
                return _MISSING, None, 0.0
            return _MISSING, _format_error(SINGLE_NO_ANSWER), None
        text = str(value)
        if text not in keys:
            return text, invalid_choice_error(text), None
        return text, None, 1.0 if text == correct else 0.0
    # multiple_choice (`pl-checkbox.py@a193f72:726-752`; adaptação de canal da escolha múltipla
    # digitada): ausente vale `[]`; o PL itera o valor como veio, sem normalizar. Numa `str`
    # (uma marcação pelo botão ou a múltipla digitada literal), cada caractere é uma marcação:
    # `set("ab")` = {a, b}; em `"b,a"` a chave inválida é a `,`; `len("aa")` conta 2 no intervalo. Numa
    # lista (a seleção pelos botões), cada elemento é uma marcação. O `submitted_answers` volta como veio.
    chosen: list[Any] = [] if value is None else list(value)
    bad = [str(k) for k in chosen if k not in keys]
    if bad:
        # O PL ecoa um elemento de `set(submitted_key) - chaves`; o dublê fixa o primeiro na ordem dada.
        return copy.deepcopy(value), invalid_option_error(bad[0]), None
    low, high = _select_bounds(f)
    selected = set(chosen)
    if not chosen and f.allow_blank:
        return (_MISSING if value is None else copy.deepcopy(value)), None, 0.0
    if not low <= len(chosen) <= high:
        return (_MISSING if value is None else copy.deepcopy(value)), select_count_error(low, high), None
    right = set(correct)
    hits, misses = len(selected & right), len(selected - right)
    if f.partial_credit == "off":
        score = 1.0 if selected == right else 0.0
    elif f.partial_credit == "coverage":
        score = (hits / len(right)) * (hits / len(selected)) if right and selected else 0.0
    elif f.partial_credit == "each-answer":
        agree = sum(1 for k in keys if (k in selected) == (k in right))
        score = agree / len(keys)
    else:  # net-correct
        score = max(0.0, (hits - misses) / len(right)) if right else 0.0
    return copy.deepcopy(value), None, score


def grade_answers(q: FakeQuestion, snapshot: Mapping[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Os campos `valid`, `format_errors`, `submitted_answers`, `partial_scores`, `score`,
    `feedback`, `params` e `correct_answers` da correção do motor de questões.

    Cada campo é interpretado como o elemento do PL o faria com o valor cru exatamente como veio
    (`_parse_field`); nada é normalizado antes. Na múltipla, uma `str` (a marcação pelo botão ou o
    texto digitado literal) vale caractere a caractere, como o `set(submitted_key)` e o
    `len(submitted_key)` do `pl-checkbox.py@a193f72:726-752` (adaptação de canal da escolha múltipla
    digitada), e o `submitted_answers` devolve a `str` como veio."""
    correct_answers = copy.deepcopy(dict(snapshot.get("correct_answers") or {}))
    submitted: dict[str, Any] = copy.deepcopy(dict(raw))
    format_errors: dict[str, dict[str, str]] = {}
    partial: dict[str, dict[str, Any]] = {}
    for f in q.fields:
        value = raw.get(f.name)
        parsed, error, score = _parse_field(f, value, correct_answers.get(f.name, f.correct))
        if parsed is _MISSING:
            submitted.pop(f.name, None)
        else:
            submitted[f.name] = parsed
        if error is not None:
            format_errors[f.name] = error
        elif score is not None:
            partial[f.name] = {"score": score, "weight": f.weight}
    score_total: float | None = None
    if not format_errors:
        if q.partial_credit:
            weight = sum(p["weight"] for p in partial.values())
            score_total = (sum(p["score"] * p["weight"] for p in partial.values()) / weight
                           if weight else 0.0)
        else:
            score_total = 1.0 if partial and all(p["score"] >= 1 for p in partial.values()) else 0.0
        if q.score_override is not None and score_total >= 1:
            score_total = q.score_override
        if q.server_score is not None:
            score_total = q.server_score
        if q.server_format_error is not None:
            format_errors[q.server_format_error.key] = _format_error(q.server_format_error.html)
    valid = not format_errors
    return {"valid": valid, "format_errors": format_errors, "submitted_answers": submitted,
            "partial_scores": partial if valid else None, "score": score_total if valid else None,
            "feedback": {}, "params": copy.deepcopy(dict(snapshot.get("params") or {})),
            "correct_answers": correct_answers}


def raw_shape_problems(ir_fields: Iterable[Mapping[str, Any]], raw: Mapping[str, Any]) -> list[str]:
    """Os desvios de `raw` das formas que o `answers.raw_for_engine` produz (na múltipla, a
    regra da escolha múltipla digitada: qualquer `str`, ou lista de 2 ou mais
    chaves distintas na ordem das opções; na única, o "Enviar" sem marcação: ausente ou `str`), sem ecoar valores (só
    o campo e a forma)."""
    problems: list[str] = []
    names = set()
    for f in ir_fields:
        name, kind = f["name"], f["type"]
        names.add(name)
        present = name in raw
        value = raw.get(name)
        if kind == "single_choice":
            # O "Enviar" sem marcação manda o campo ausente,
            # como o PL recebe o formulário sem rádio marcado; presente, só `str` (a chave marcada ou o texto
            # digitado literal). O `null` presente é desvio: o `raw_for_engine` pula o `None` das `parts`.
            if present and not isinstance(value, str):
                problems.append(f"campo {name} (single_choice): esperado ausente ou str")
            continue
        if kind in ("number", "integer", "text"):
            if not present or not isinstance(value, str):
                problems.append(f"campo {name} ({kind}): esperado presente e str")
            continue
        # multiple_choice (adaptação de canal da escolha múltipla digitada): ausente
        # (seleção vazia pelos botões); qualquer `str` (uma marcação pelo botão, ou a múltipla digitada,
        # que vai literal: `""`, `","`, `"b,a"`, `"ab"`, `"z"`; quem recusa é o PL, com o próprio erro de
        # formato); ou lista de 2 ou mais chaves distintas na ordem das opções (a seleção pelos botões).
        # A lista com algum token que não é chave é recusada.
        keys = [o["key"] for o in (f.get("options") or [])]
        if not present or isinstance(value, str):
            continue
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            problems.append(f"campo {name} (multiple_choice): esperado ausente, str ou lista de str")
        elif not all(v in keys for v in value):
            problems.append(f"campo {name} (multiple_choice): lista com token que não é chave "
                            f"(a múltipla digitada vai como str literal)")
        elif len(value) < 2:
            problems.append(f"campo {name} (multiple_choice): uma só chave vai como str, não lista")
        elif len(set(value)) != len(value):
            problems.append(f"campo {name} (multiple_choice): chaves repetidas")
        elif value != [k for k in keys if k in value]:
            problems.append(f"campo {name} (multiple_choice): chaves fora da ordem das opções")
    extra = sorted(set(raw) - names)
    if extra:
        problems.append(f"{len(extra)} campo(s) fora da IR")
    return problems


# ---------------------------------------------------------------------------------------------
# Tipos públicos da observação
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineCall:
    """Um pedido recebido pelo dublê.

    `op` é a operação da rota (`OPS`) ou `""` fora delas. `request` é o corpo JSON (`None` sem corpo
    ou se não é JSON); `response` é o corpo JSON enviado (`None` se não houve resposta JSON).
    `outcome`: `pending` (aberto), `ok` (2xx), `error` (4xx/5xx), `network` (falha de rede programada
    com alguma resposta), `reset`, `hang` (resposta presa até ser solta ou o cliente desistir) ou
    `abandoned` (o cliente foi embora durante a espera). `programmed` diz que a resposta veio de
    `fail`, `inject`, `fail_network`, `slow` ou `inject_version_mismatch`. `started`/`ended` são de
    `monotonic()`; `connection_id` distingue as conexões TCP.
    """

    seq: int
    op: str
    http_method: str
    path: str
    has_query: bool
    content_type: str | None
    connection_id: int
    request: Any
    status: int | None
    response: Any
    outcome: str
    programmed: bool
    started: float
    ended: float | None


@dataclass(frozen=True)
class IssuedVariant:
    """Uma variante `ok` emitida pelo dublê (`issued_variants()`)."""

    repo: str
    commit: str
    qid: str
    seed: str
    snapshot: dict[str, Any]
    ir: dict[str, Any]


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class _Call:
    def __init__(self, *, seq: int, gen: int, op: str, http_method: str, path: str, has_query: bool,
                 content_type: str | None, connection_id: int, request: Any, started: float) -> None:
        self.seq = seq
        self.gen = gen
        self.op = op
        self.http_method = http_method
        self.path = path
        self.has_query = has_query
        self.content_type = content_type
        self.connection_id = connection_id
        self.request = request
        self.started = started
        self.ended: float | None = None
        self.status: int | None = None
        self.response: Any = None
        self.outcome = "pending"
        self.programmed = False

    def snapshot(self) -> EngineCall:
        return EngineCall(seq=self.seq, op=self.op, http_method=self.http_method, path=self.path,
                          has_query=self.has_query, content_type=self.content_type,
                          connection_id=self.connection_id, request=copy.deepcopy(self.request),
                          status=self.status, response=copy.deepcopy(self.response),
                          outcome=self.outcome, programmed=self.programmed, started=self.started,
                          ended=self.ended)


@dataclass
class _Failure:
    op: str
    kind: str  # http | inject | <NETWORK_KINDS>
    times: int | None
    when: Callable[[EngineCall], bool] | None
    status: int | None = None
    body: Any = None
    raw: bytes | None = None
    content_type: str = "application/json; charset=utf-8"
    off_contract: bool = False


@dataclass
class _Slow:
    op: str
    seconds: float
    drip: bool
    times: int | None
    when: Callable[[EngineCall], bool] | None


@dataclass
class _Mismatch:
    engine: bool
    pl: bool
    times: int | None
    op: str = "grade"
    when: Callable[[EngineCall], bool] | None = None


@dataclass
class _Health:
    status: int = 200
    contract_version: str = CONTRACT_VERSION
    ir_version: str = IR_VERSION
    workers: int = WORKERS


def _client_gone(sock: socket.socket) -> bool:
    """O cliente fechou a conexão? (`select` + `recv(1, MSG_PEEK)`, sem bloquear nem consumir.)"""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


# ---------------------------------------------------------------------------------------------
# Servidor HTTP
# ---------------------------------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = os.name != "nt"  # no Windows, SO_REUSEADDR deixaria outro processo roubar a porta
    request_queue_size = 128
    fake: FakeEngine

    def server_bind(self) -> None:
        # Sem `socket.getfqdn` (o `HTTPServer.server_bind` faz uma busca de DNS que pode demorar).
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Silêncio: o padrão imprime o traceback em stderr."""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeEngine/1.0"
    sys_version = ""
    timeout = KEEPALIVE_SECONDS  # conexão ociosa fecha em 5 s, como o motor
    server: _Server
    connection_id: int
    fake_call: _Call | None = None

    def setup(self) -> None:
        super().setup()
        self.connection_id = self.server.fake._register_connection(self.connection)

    def handle(self) -> None:
        try:
            super().handle()
        except OSError:
            self.close_connection = True

    def finish(self) -> None:
        try:
            super().finish()
        except OSError:
            pass
        finally:
            self.server.fake._unregister_connection(self.connection)

    def _dispatch(self) -> None:
        self.server.fake._serve(self)

    do_POST = do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - assinatura da base
        """Silêncio."""

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002 - assinatura da base
        """Silêncio."""

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        """Silêncio."""


def _read_body(h: _Handler) -> tuple[bytes, bool, list[str]]:
    """(corpo, acima de 2 MiB?, desvios). Acima do limite, lê e descarta o resto, como o motor."""
    problems: list[str] = []
    encoding = (h.headers.get("Transfer-Encoding") or "").strip().lower()
    chunks: list[bytes] = []
    size = 0
    if encoding and encoding != "identity":
        problems.append("corpo com Transfer-Encoding (esperado Content-Length)")
        if "chunked" not in encoding:
            h.close_connection = True
            return b"", False, problems
        while True:
            size_line = h.rfile.readline(65537).split(b";")[0].strip()
            try:
                length = int(size_line, 16)
            except ValueError:
                h.close_connection = True
                break
            if length == 0:
                while h.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                    pass
                break
            data = h.rfile.read(length)
            size += len(data)
            if size <= MAX_BODY_BYTES:
                chunks.append(data)
            h.rfile.readline(65537)
        return (b"" if size > MAX_BODY_BYTES else b"".join(chunks)), size > MAX_BODY_BYTES, problems
    declared = h.headers.get("Content-Length")
    if declared is None:
        return b"", False, problems
    try:
        total = int(declared)
        if total < 0:
            raise ValueError
    except ValueError:
        problems.append("Content-Length inválido")
        h.close_connection = True
        return b"", False, problems
    remaining = total
    while remaining > 0:
        data = h.rfile.read(min(remaining, 1 << 16))
        if not data:
            h.close_connection = True
            break
        remaining -= len(data)
        size += len(data)
        if size <= MAX_BODY_BYTES:
            chunks.append(data)
    too_large = total > MAX_BODY_BYTES
    return (b"" if too_large else b"".join(chunks)), too_large, problems


def _reject_constant(name: str) -> Any:
    raise ValueError(f"constante não JSON: {name}")


# ---------------------------------------------------------------------------------------------
# O motor falso
# ---------------------------------------------------------------------------------------------


class FakeEngine:
    """Motor falso estrito (ver o docstring do módulo).

    Uso típico: `fake.set_repos_root(raiz)` (a mesma da obtenção falsa), `fake.program("number",
    preset("number"))` (ou `program_presets()`), o sistema recebe `fake.base_url` (`CQ_ENGINE_URL`),
    e o teste confere `calls()`, `issued_variants()` e `protocol_errors()`. `reset()` limpa tudo entre
    testes.
    """

    def __init__(self, *, repos_root: str | os.PathLike[str] | None = None) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._gen = 0
        self._closed = False
        self._stopped = True
        self._conn_ids = itertools.count(1)
        self._connections: set[socket.socket] = set()
        self._active = 0
        self._init_state(repos_root=repos_root, answer_sentinel=ANSWER_SENTINEL)
        self.port = 0
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.start()
        self.base_url: str = f"http://127.0.0.1:{self.port}"

    def _init_state(self, *, repos_root: str | os.PathLike[str] | None, answer_sentinel: str) -> None:
        self.repos_root: pathlib.Path | None = None if repos_root is None else pathlib.Path(repos_root)
        self.answer_sentinel = answer_sentinel
        self._questions: dict[tuple[str, ...], FakeQuestion] = {}
        self._issued: list[IssuedVariant] = []
        self._issued_keys: dict[tuple[str, str, str], set[str]] = {}
        self._memo: dict[tuple[str, str], str] = {}
        self._calls: list[_Call] = []
        self._seq = itertools.count(1)
        self._failures: list[_Failure] = []
        self._slows: list[_Slow] = []
        self._mismatches: list[_Mismatch] = []
        self._problems: list[str] = []
        self._health = _Health()
        self._engine_version = ENGINE_VERSION
        self._pl_commit = PL_COMMIT
        self._raw_shape = True
        self._foreign_snapshots = False
        self._last_seq = 0
        self._released_through = 0
        self._seen_connections: set[int] = set()

    # -- ciclo de vida -------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stopped(self) -> bool:
        return self._stopped

    def reset(self, *, repos_root: str | os.PathLike[str] | None = None,
              answer_sentinel: str = ANSWER_SENTINEL) -> None:
        """Esquece programação, variantes emitidas, chamadas, falhas, desvios e versões; religa o
        servidor se estava parado. As respostas presas terminam sem resposta."""
        with self._cond:
            self._gen += 1
            self._init_state(repos_root=repos_root, answer_sentinel=answer_sentinel)
            self._cond.notify_all()
        if self._stopped and not self._closed:
            self.start()

    def set_repos_root(self, repos_root: str | os.PathLike[str] | None) -> None:
        """A raiz da área de repositórios (a mesma da obtenção falsa)."""
        with self._cond:
            self.repos_root = None if repos_root is None else pathlib.Path(repos_root)

    def start(self) -> None:
        """Religa o servidor na **mesma** porta (ou numa livre, na primeira vez)."""
        with self._cond:
            if self._closed:
                raise RuntimeError("motor falso fechado")
            if not self._stopped:
                return
        deadline = monotonic() + 5.0
        while True:
            try:
                server = _Server(("127.0.0.1", self.port), _Handler)
                break
            except OSError:
                if self.port == 0 or monotonic() > deadline:
                    raise
                time.sleep(0.05)
        server.fake = self
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="fake-engine", daemon=True)
        with self._cond:
            self._stopped = False
        self._thread.start()

    def stop(self) -> None:
        """Para de aceitar conexões (o cliente vê conexão recusada) e derruba as abertas; as
        respostas presas terminam sem resposta. `start()` religa na mesma porta."""
        with self._cond:
            if self._stopped:
                return
            self._stopped = True
            self._cond.notify_all()
        server, thread = self._server, self._thread
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(5)
        self._drop_connections()

    def close(self) -> None:
        """Para o servidor de vez (idempotente)."""
        if self._closed:
            return
        self.stop()
        self._closed = True

    def _drop_connections(self) -> None:
        deadline = monotonic() + 2.0
        with self._cond:
            while self._active and monotonic() < deadline:
                self._cond.wait(0.01)
            connections = list(self._connections)
        for sock in connections:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def __enter__(self) -> FakeEngine:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- programação ---------------------------------------------------------------------------

    @staticmethod
    def _key(target: Any) -> tuple[str, ...]:
        if isinstance(target, str):
            return ("qid", target)
        if isinstance(target, tuple) and len(target) == 3:
            return ("ref", str(target[0]), str(target[1]), str(target[2]))
        if all(hasattr(target, attr) for attr in ("repo", "commit", "qid")):
            return ("ref", str(target.repo), str(target.commit), str(target.qid))
        raise TypeError("program: use um QID (str), (repo, commit, qid) ou um objeto com repo/commit/qid")

    def program(self, target: Any, question: FakeQuestion | str) -> None:
        """Programa a questão de `target`: um QID (vale para qualquer repositório e commit), a tupla
        `(repo, commit, qid)` ou um `QuestionRef`; `question` é um `FakeQuestion` ou o nome de um
        preset. A referência exata vence o QID."""
        q = preset(question) if isinstance(question, str) else question
        with self._cond:
            self._questions[self._key(target)] = q
            self._memo.clear()

    def program_presets(self) -> None:
        """Programa cada preset de `IDENTITY_FREE_PRESETS` sob o QID de mesmo nome (as questões de
        `tests/fixtures/repos/course/`)."""
        for name in IDENTITY_FREE_PRESETS:
            self.program(name, preset(name))

    def expect_raw_shape(self, enabled: bool) -> None:
        """Liga (padrão) ou desliga a conferência da resposta crua contra as formas do
        `raw_for_engine`."""
        with self._cond:
            self._raw_shape = bool(enabled)

    def accept_foreign_snapshots(self, enabled: bool = True) -> None:
        """Aceita no `grade` snapshots que o dublê não emitiu (teste de diferença de versão)."""
        with self._cond:
            self._foreign_snapshots = bool(enabled)

    def set_versions(self, *, engine_version: str | None = None, pl_commit: str | None = None) -> None:
        """Troca a versão do motor e/ou o commit do PL (no `health`, nos snapshots novos e na
        comparação do `version_mismatch` da correção)."""
        with self._cond:
            if engine_version is not None:
                self._engine_version = engine_version
            if pl_commit is not None:
                self._pl_commit = pl_commit
            self._memo.clear()

    def set_health(self, *, status: int = 200, contract_version: str = CONTRACT_VERSION,
                   ir_version: str = IR_VERSION, workers: int = WORKERS) -> None:
        """O `GET /v1/health`: `status=500` dá `internal_error` (sem executor Python); versões do
        contrato ou da IR diferentes de `1.0` simulam um motor de outro contrato (desvio declarado:
        o resto do corpo continua validado). Sem argumentos, volta ao normal."""
        if status not in (200, 500):
            raise ValueError("set_health: status deve ser 200 ou 500")
        with self._cond:
            self._health = _Health(status=status, contract_version=contract_version,
                                   ir_version=ir_version, workers=workers)

    def inject_version_mismatch(self, *, engine: bool = True, pl: bool = True,
                                times: int | None = None) -> None:
        """As próximas `times` correções `ok`/`broken` (`None` = até `clear_failures`) saem com
        `version_mismatch = {engine, pl}`."""
        self._check_times(times)
        with self._cond:
            self._mismatches.append(_Mismatch(engine=engine, pl=pl, times=times))

    # -- falhas programadas --------------------------------------------------------------------

    @staticmethod
    def _check_op(op: str) -> None:
        if op != "*" and op not in OPS:
            raise ValueError(f"operação fora do motor: {op}")

    @staticmethod
    def _check_times(times: int | None) -> None:
        if times is not None and (isinstance(times, bool) or not isinstance(times, int) or times < 1):
            raise ValueError("times deve ser um inteiro positivo ou None")

    def fail(self, op: str, *, error: str | int, times: int | None = 1,
             when: Callable[[EngineCall], bool] | None = None, message: str | None = None,
             details: list[str] | None = None) -> None:
        """As próximas `times` chamadas a `op` (`"*"` = qualquer) que casam com `when(call)` recebem o
        erro do contrato: `error` é o código (`ERROR_STATUS`) ou o status (`ERROR_OF_STATUS`; 404 é
        `not_found`). A resposta passa pela validação como qualquer outra."""
        self._check_op(op)
        self._check_times(times)
        code = ERROR_OF_STATUS.get(error) if isinstance(error, int) else error
        if code not in ERROR_STATUS:
            raise ValueError(f"fail: erro desconhecido: {error}")
        body: dict[str, Any] = {"code": code, "message": ERROR_MESSAGES[code] if message is None else message}
        if details:
            body["details"] = list(details)
        with self._cond:
            self._failures.append(_Failure(op=op, kind="http", times=times, when=when,
                                           status=ERROR_STATUS[code], body={"error": body}))

    def inject(self, op: str, *, body: Any, status: int = 200, times: int | None = 1,
               when: Callable[[EngineCall], bool] | None = None, off_contract: bool = False) -> None:
        """Responde `body` (JSON) com `status` nas próximas `times` chamadas a `op`. A resposta é
        validada como as outras (fora do esquema → erro de protocolo), salvo `off_contract=True`, o
        desvio declarado para testar o cliente contra um motor fora do contrato."""
        self._check_op(op)
        self._check_times(times)
        with self._cond:
            self._failures.append(_Failure(op=op, kind="inject", times=times, when=when, status=status,
                                           body=copy.deepcopy(body), off_contract=off_contract))

    def fail_network(self, op: str, *, kind: str, times: int | None = 1,
                     when: Callable[[EngineCall], bool] | None = None) -> None:
        """Falha de rede nas próximas chamadas (`NETWORK_KINDS`):
        - `reset`: corta a conexão com RST, sem resposta;
        - `hang`: não responde até `release_hangs`, `reset`, `stop` ou o cliente desistir (aí fecha
          sem resposta);
        - `not_json`: 200 com corpo que não é JSON; `html_502`: 502 com a página HTML de um proxy;
        - `truncated`: `Content-Length` maior que o corpo enviado, e a conexão fecha;
        - `oversize`: 200 com `Content-Length` = 64 MiB + 1 e o corpo enviado até o cliente fechar;
          `oversize_chunked`: o mesmo sem `Content-Length` (em blocos) até passar de 64 MiB;
        - `redirect`: 307 para a mesma rota; `nan`: 200 com `NaN` no JSON.
        """
        self._check_op(op)
        self._check_times(times)
        if kind not in NETWORK_KINDS:
            raise ValueError(f"kind deve ser um de {NETWORK_KINDS}")
        with self._cond:
            self._failures.append(_Failure(op=op, kind=kind, times=times, when=when))

    def slow(self, op: str, seconds: float, *, drip: bool = False, times: int | None = 1,
             when: Callable[[EngineCall], bool] | None = None) -> None:
        """Atrasa a resposta das próximas chamadas a `op` em `seconds` (tempo real). Com `drip`, os
        cabeçalhos saem logo e o corpo goteja ao longo de `seconds`."""
        self._check_op(op)
        self._check_times(times)
        if seconds < 0:
            raise ValueError("seconds não pode ser negativo")
        with self._cond:
            self._slows.append(_Slow(op=op, seconds=float(seconds), drip=drip, times=times, when=when))

    def release_hangs(self) -> None:
        """Solta as respostas presas de todas as chamadas já recebidas (fecha sem resposta)."""
        with self._cond:
            self._released_through = self._last_seq
            self._cond.notify_all()

    def clear_failures(self) -> None:
        """Remove falhas, atrasos e diferenças de versão programados ainda não usados."""
        with self._cond:
            self._failures.clear()
            self._slows.clear()
            self._mismatches.clear()

    # -- observação ----------------------------------------------------------------------------

    def calls(self, op: str | None = None) -> list[EngineCall]:
        with self._cond:
            return [c.snapshot() for c in self._calls if op is None or c.op == op]

    def connection_count(self) -> int:
        """Quantas conexões TCP distintas levaram pedidos desde o `reset`."""
        with self._cond:
            return len(self._seen_connections)

    def issued_variants(self, qid: str | None = None) -> list[IssuedVariant]:
        with self._cond:
            return [copy.deepcopy(v) for v in self._issued if qid is None or v.qid == qid]

    def protocol_errors(self) -> list[str]:
        with self._cond:
            return list(self._problems)

    def wait_for(self, predicate: Callable[[], T], *, timeout: float = 5.0) -> T:
        """Espera `predicate()` ficar verdadeiro e devolve o valor; `AssertionError` no tempo."""
        deadline = monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AssertionError(f"wait_for: condição não satisfeita em {timeout} s")
            with self._cond:
                self._cond.wait(min(SOCKET_CHECK_SECONDS, remaining))

    # -- cliente do lado do teste --------------------------------------------------------------

    def request(self, op_or_path: str, body: Any = None, *, http_method: str | None = None,
                raw: bytes | None = None, content_type: str | None = "application/json",
                query: str = "", timeout: float = 10.0) -> Reply:
        """Um pedido ao dublê como um cliente qualquer: `op_or_path` é uma operação (`OPS`) ou um
        caminho; sem `raw`, `body` vai em JSON estrito."""
        path = PATH_OF.get(op_or_path, op_or_path)
        method = http_method or ("GET" if path == "/v1/health" else "POST")
        data = raw
        if data is None and body is not None:
            data = json.dumps(body, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        headers = {"Content-Type": content_type} if data is not None and content_type else {}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path + (f"?{query}" if query else ""), body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            return Reply(resp.status, {k.lower(): v for k, v in resp.getheaders()}, payload)
        finally:
            conn.close()

    # -- internos: conexões e registro ---------------------------------------------------------

    def _register_connection(self, sock: socket.socket) -> int:
        with self._cond:
            self._connections.add(sock)
            return next(self._conn_ids)

    def _unregister_connection(self, sock: socket.socket) -> None:
        with self._cond:
            self._connections.discard(sock)

    def _problem(self, gen: int, text: str) -> None:
        with self._cond:
            if gen == self._gen:
                self._problems.append(text)

    def _finish(self, call: _Call, outcome: str, *, status: int | None = None, response: Any = None,
                programmed: bool = False) -> None:
        with self._cond:
            call.ended = monotonic()
            call.outcome = outcome
            call.status = status
            call.response = copy.deepcopy(response)
            call.programmed = call.programmed or programmed
            self._cond.notify_all()

    def _wait(self, h: _Handler, call: _Call, *, until: float | None) -> bool:
        """Espera até `until` (ou até ser solta, com `None`); `False` se o cliente foi embora, veio
        `reset`/`stop`/`close` ou (sem prazo) `release_hangs`."""
        while True:
            with self._cond:
                if call.gen != self._gen or self._stopped:
                    return False
                if until is None and call.seq <= self._released_through:
                    return False
                remaining = None if until is None else until - monotonic()
                if remaining is not None and remaining <= 0:
                    return True
                self._cond.wait(SOCKET_CHECK_SECONDS if remaining is None
                                else min(SOCKET_CHECK_SECONDS, remaining))
            if _client_gone(h.connection):
                return False

    # -- internos: atendimento -----------------------------------------------------------------

    def _serve(self, h: _Handler) -> None:
        started = monotonic()
        with self._cond:
            self._active += 1
            gen = self._gen
        h.fake_call = None
        try:
            self._serve_inner(h, started)
        except Exception as exc:  # noqa: BLE001 - erro do próprio dublê: acusado, nunca impresso
            call = h.fake_call
            prefix = f"{call.op or '?'}: " if call is not None else ""
            self._problem(gen, f"{prefix}erro interno do motor falso: {type(exc).__name__}: {exc}")
            h.close_connection = True
            body = {"error": {"code": "internal_error", "message": ERROR_MESSAGES["internal_error"]}}
            if call is not None and call.ended is None:
                self._finish(call, "error", status=500, response=body)
            self._write(h, 500, json.dumps(body).encode("utf-8"))
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def _serve_inner(self, h: _Handler, started: float) -> None:
        split = urllib.parse.urlsplit(h.path)
        body, too_large, problems = _read_body(h)
        content_type = h.headers.get("Content-Type")
        media = (content_type or "").split(";")[0].strip().lower()
        route = ROUTES.get(split.path)
        op = route[0] if route else ""
        request: Any = None
        early: tuple[int, dict[str, Any], tuple[tuple[str, str], ...]] | None = None
        if route is None:
            problems.append("rota fora de GET /v1/health e POST /v1/inspect, /v1/variants e /v1/grade")
            early = (404, self._error_body("route_not_found"), ())
        elif h.command != route[1]:
            problems.append(f"método HTTP {h.command} (esperado {route[1]})")
            early = (405, self._error_body("method_not_allowed"), (("Allow", route[1]),))
        if split.query:
            problems.append("parâmetros na consulta da URL")
        if early is None and route is not None:
            if route[1] == "GET":
                if body:
                    problems.append("corpo no GET /v1/health")
            elif too_large:
                problems.append("corpo acima de 2 MiB (o cliente deveria recusar antes de enviar)")
                early = (413, self._error_body("payload_too_large"), ())
            else:
                if media != "application/json":
                    problems.append(f"Content-Type {media or 'ausente'} (esperado application/json)")
                charset = re.search(r"charset=([^;\s]+)", content_type or "", re.IGNORECASE)
                if charset and charset.group(1).strip("\"'").lower() not in ("utf-8", "utf8"):
                    problems.append("corpo JSON fora de UTF-8")
                try:
                    request = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
                except (UnicodeDecodeError, ValueError):
                    problems.append("corpo não é JSON UTF-8 estrito")
                    early = (400, {"error": {"code": "invalid_request", "message": INVALID_JSON_MESSAGE,
                                             "details": [INVALID_JSON_DETAIL]}}, ())
        with self._cond:
            gen = self._gen
            seq = next(self._seq)
            call = _Call(seq=seq, gen=gen, op=op, http_method=h.command, path=split.path,
                         has_query=bool(split.query), content_type=content_type,
                         connection_id=h.connection_id, request=copy.deepcopy(request), started=started)
            self._calls.append(call)
            self._last_seq = seq
            self._seen_connections.add(h.connection_id)
            h.fake_call = call
            for problem in problems:
                self._problems.append(f"{op or '?'}: {problem}")
            self._cond.notify_all()
        if early is not None:
            status, payload, headers = early
            return self._respond(h, call, status, payload, headers=headers)
        failure = self._take(self._failures, call)
        slow = self._take(self._slows, call)
        if failure is not None and failure.kind not in ("http", "inject"):
            return self._network(h, call, failure)
        if failure is not None:
            return self._respond(h, call, int(failure.status or 200), failure.body, programmed=True,
                                 off_contract=failure.off_contract, slow=slow)
        status, payload, headers, programmed, relaxed = self._operation(call)
        return self._respond(h, call, status, payload, headers=headers, programmed=programmed,
                             relaxed=relaxed, slow=slow)

    def _take(self, items: list[Any], call: _Call) -> Any:
        with self._cond:
            if call.gen != self._gen:
                return None
            snapshot = call.snapshot()
            for item in items:
                if item.op not in ("*", call.op):
                    continue
                if item.when is not None:
                    try:
                        if not item.when(snapshot):
                            continue
                    except Exception as exc:  # noqa: BLE001 - vira erro de protocolo
                        self._problems.append(f"{call.op}: predicado when falhou: {type(exc).__name__}")
                        continue
                if item.times is not None:
                    item.times -= 1
                    if item.times == 0:
                        items.remove(item)
                return copy.copy(item)
            return None

    @staticmethod
    def _error_body(code: str, message: str | None = None, details: list[str] | None = None,
                    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": ERROR_MESSAGES[code] if message is None else message}
        if details:
            error["details"] = details
        return {"error": error}

    # -- operações -----------------------------------------------------------------------------

    def _operation(self, call: _Call) -> tuple[int, Any, tuple[tuple[str, str], ...], bool, bool]:
        """(status, corpo, cabeçalhos, programada?, desvio declarado da saúde?)."""
        if call.op == "health":
            with self._cond:
                health = copy.copy(self._health)
                engine_version, pl_commit = self._engine_version, self._pl_commit
            if health.status == 500:
                return 500, self._error_body("internal_error"), (), True, False
            relaxed = (health.contract_version, health.ir_version) != (CONTRACT_VERSION, IR_VERSION)
            return 200, {"status": "ready", "engine_version": engine_version,
                         "contract_version": health.contract_version, "ir_version": health.ir_version,
                         "pl_commit": pl_commit, "workers": health.workers}, (), relaxed, relaxed
        request = call.request
        errors = schema_errors(REQUEST_DEF[call.op], request)
        if errors:
            self._problem(call.gen, f"{call.op}: pedido fora do esquema: {'; '.join(errors[:5])}")
            return 400, self._error_body("invalid_request", details=errors[:20]), (), False, False
        seed = request["seed"] if call.op == "create_variant" else (
            request["snapshot"]["seed"] if call.op == "grade" else None)
        if seed is not None and seed_value(seed) >= SEED_LIMIT:
            self._problem(call.gen, f"{call.op}: seed ≥ 2^32 (o cliente deveria recusar antes de enviar)")
            return 400, self._error_body("invalid_request", SEED_TOO_LARGE_MESSAGE), (), False, False
        ref = request["question_ref"]
        repo, commit, qid = ref["repo"], ref["commit"], ref["qid"]
        missing, question_dir = self._resolve(repo, commit, qid)
        if missing is not None:
            return 404, self._error_body("not_found", missing), (), False, False
        with self._cond:
            q = self._questions.get(("ref", repo, commit, qid)) or self._questions.get(("qid", qid))
            sentinel = self.answer_sentinel
            engine_version, pl_commit = self._engine_version, self._pl_commit
        if q is None:
            self._problem(call.gen, f"{call.op}: questão no disco sem programação no motor falso")
            return 500, self._error_body("internal_error"), (), False, False
        if call.op == "inspect":
            assert question_dir is not None
            return 200, self._inspect(q, question_dir), (), False, False
        if call.op == "create_variant":
            return 200, self._create_variant(call, q, repo, commit, qid, seed, sentinel, engine_version,
                                             pl_commit), (), False, False
        payload, programmed = self._grade(call, q, repo, commit, qid, request, engine_version, pl_commit)
        return 200, payload, (), programmed, False

    def _resolve(self, repo: str, commit: str, qid: str) -> tuple[str | None, pathlib.Path | None]:
        with self._cond:
            root = self.repos_root
        if root is None:
            return NOT_FOUND_REPOSITORY, None
        repo_dir = root / repo_key(repo)
        if not repo_dir.is_dir():
            return NOT_FOUND_REPOSITORY, None
        course = repo_dir / commit
        if not course.is_dir():
            return NOT_FOUND_COMMIT, None
        question_dir = course / "questions" / pathlib.PurePosixPath(qid)
        if not question_dir.is_dir():
            return NOT_FOUND_QUESTION, None
        if not (question_dir / "info.json").is_file():
            return NOT_FOUND_INFO_JSON, None
        return None, question_dir

    def _inspect(self, q: FakeQuestion, question_dir: pathlib.Path) -> dict[str, Any]:
        elements = sorted({ELEMENT_OF[f.type] for f in q.fields} | set(q.extra_elements))
        question = {"uuid": q.uuid, "title": q.title, "topic": q.topic, "tags": list(q.tags), "type": "v3",
                    "grading_method": "Internal", "elements": elements,
                    "show_correct_answer": q.show_correct_answer, "single_variant": q.single_variant,
                    "partial_credit": q.partial_credit, "answers_names": [f.name for f in q.fields],
                    "content_hash": question_content_hash(question_dir)}
        reasons = [_reason(r) for r in q.inspect_reasons]
        return {"outcome": "unsupported" if reasons else "supported", "question": question,
                "reasons": reasons}

    def _create_variant(self, call: _Call, q: FakeQuestion, repo: str, commit: str, qid: str, seed: str,
                        sentinel: str, engine_version: str, pl_commit: str) -> dict[str, Any]:
        if q.inspect_reasons or q.variant_reasons:
            return {"outcome": "unsupported", "reasons": [_reason(r) for r in (q.inspect_reasons
                                                                                or q.variant_reasons)]}
        if q.variant_broken is not None:
            return {"outcome": "broken", "reason": _broken(q.variant_broken),
                    "warnings": [_warning(w) for w in q.variant_warnings]}
        snapshot, ir, warnings = build_variant(q, seed, sentinel=sentinel, engine_version=engine_version,
                                               pl_commit=pl_commit)
        with self._cond:
            if call.gen == self._gen:
                key = canonical(snapshot)
                seen = self._issued_keys.setdefault((repo, commit, qid), set())
                if key not in seen:
                    seen.add(key)
                    self._issued.append(IssuedVariant(repo=repo, commit=commit, qid=qid, seed=seed,
                                                      snapshot=copy.deepcopy(snapshot), ir=copy.deepcopy(ir)))
        return {"outcome": "ok", "snapshot": snapshot, "ir": ir, "warnings": warnings}

    def _grade(self, call: _Call, q: FakeQuestion, repo: str, commit: str, qid: str,
               request: Mapping[str, Any], engine_version: str, pl_commit: str) -> tuple[dict[str, Any], bool]:
        snapshot = request["snapshot"]
        raw = request["raw_submitted_answers"]
        with self._cond:
            foreign_ok = self._foreign_snapshots
            raw_check = self._raw_shape
            issued = canonical(snapshot) in self._issued_keys.get((repo, commit, qid), set())
        if not issued and not foreign_ok:
            self._problem(call.gen, "grade: snapshot que o motor falso não emitiu para esta referência")
        _snapshot, ir, _warnings = build_variant(q, snapshot["seed"])
        if raw_check:
            for problem in raw_shape_problems(ir["fields"], raw):
                self._problem(call.gen, f"grade: resposta crua fora das formas do raw_for_engine: {problem}")
        forced = self._take(self._mismatches, call)
        if q.version_mismatch is not None:
            mismatch = {"engine": q.version_mismatch[0], "pl": q.version_mismatch[1]}
        else:
            mismatch = {"engine": snapshot.get("engine_version") != engine_version,
                        "pl": snapshot.get("pl_commit") != pl_commit}
        if forced is not None:
            mismatch = {"engine": forced.engine, "pl": forced.pl}
        warnings = [_warning(w) for w in q.grade_warnings]
        if q.grade_broken is not None:
            return ({"outcome": "broken", "reason": _broken(q.grade_broken), "version_mismatch": mismatch,
                     "warnings": warnings}, forced is not None)
        result = grade_answers(q, snapshot, raw)
        return ({"outcome": "ok", **result, "version_mismatch": mismatch, "warnings": warnings},
                forced is not None)

    # -- respostas -----------------------------------------------------------------------------

    def _response_problems(self, op: str, status: int, payload: Any, *, relaxed: bool) -> list[str]:
        if status == 200 and op in RESPONSE_DEF:
            instance = payload
            if relaxed and op == "health" and isinstance(payload, dict):
                instance = {**payload, "contract_version": CONTRACT_VERSION, "ir_version": IR_VERSION}
            return schema_errors(RESPONSE_DEF[op], instance)
        found = schema_errors("error_response", payload)
        code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
        if isinstance(code, str) and ERROR_STATUS.get(code) != status:
            found.append(f"status {status} incoerente com o código {code}")
        if status == 200:
            found.append("200 com corpo de erro")
        return found

    def _respond(self, h: _Handler, call: _Call, status: int, payload: Any, *,
                 headers: tuple[tuple[str, str], ...] = (), programmed: bool = False,
                 off_contract: bool = False, relaxed: bool = False, slow: _Slow | None = None) -> None:
        if not off_contract:
            problems = self._response_problems(call.op, status, payload, relaxed=relaxed)
            if problems:
                self._problem(call.gen, f"{call.op or '?'}: resposta do motor falso fora do contrato: "
                                        f"{'; '.join(problems[:5])}")
        if status == 200 and not programmed and call.op in REQUEST_DEF and call.request is not None:
            key = (call.op, canonical(call.request))
            value = canonical(payload)
            with self._cond:
                previous = self._memo.setdefault(key, value) if call.gen == self._gen else value
            if previous != value:
                self._problem(call.gen, f"{call.op}: resposta diferente para o mesmo pedido (determinismo)")
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        outcome = "ok" if 200 <= status < 300 else "error"
        if slow is not None and not slow.drip:
            if not self._wait(h, call, until=monotonic() + slow.seconds):
                self._finish(call, "abandoned", programmed=True)
                h.close_connection = True
                return
        self._finish(call, outcome, status=status, response=payload,
                     programmed=programmed or slow is not None)
        self._write(h, status, data, headers=headers,
                    drip=slow.seconds if slow is not None and slow.drip else None, call=call)

    def _write(self, h: _Handler, status: int, data: bytes, *, content_type: str = "application/json; "
               "charset=utf-8", headers: tuple[tuple[str, str], ...] = (), drip: float | None = None,
               call: _Call | None = None, length: int | None = None) -> None:
        try:
            h.send_response(status)
            h.send_header("Content-Type", content_type)
            h.send_header("Content-Length", str(len(data) if length is None else length))
            for name, value in headers:
                h.send_header(name, value)
            if h.close_connection:
                h.send_header("Connection", "close")
            else:
                h.send_header("Connection", "keep-alive")
                h.send_header("Keep-Alive", f"timeout={int(KEEPALIVE_SECONDS)}")
            h.end_headers()
            if drip is None or call is None:
                h.wfile.write(data)
            else:
                pieces = 10
                step = max(1, -(-len(data) // pieces))
                for index in range(0, len(data), step):
                    h.wfile.write(data[index:index + step])
                    h.wfile.flush()
                    if not self._wait(h, call, until=monotonic() + drip / pieces):
                        h.close_connection = True
                        return
            h.wfile.flush()
        except OSError:
            h.close_connection = True

    def _network(self, h: _Handler, call: _Call, failure: _Failure) -> None:
        kind = failure.kind
        if kind == "reset":
            self._finish(call, "reset", programmed=True)
            h.close_connection = True
            try:
                linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
                h.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                h.connection.close()
            except OSError:
                pass
            return
        if kind == "hang":
            self._wait(h, call, until=None)
            self._finish(call, "hang", programmed=True)
            h.close_connection = True
            return
        if kind == "not_json":
            self._finish(call, "network", status=200, programmed=True)
            return self._write(h, 200, b"isto nao e JSON")
        if kind == "html_502":
            self._finish(call, "network", status=502, programmed=True)
            return self._write(h, 502, HTML_BAD_GATEWAY, content_type="text/html")
        if kind == "nan":
            self._finish(call, "network", status=200, programmed=True)
            return self._write(h, 200, b'{"outcome":"ok","score":NaN}')
        if kind == "redirect":
            self._finish(call, "network", status=307, programmed=True)
            location = f"http://127.0.0.1:{self.port}{call.path}"
            return self._write(h, 307, b"{}", headers=(("Location", location),))
        if kind == "truncated":
            self._finish(call, "network", status=200, programmed=True)
            h.close_connection = True
            data = b'{"outcome":"ok","snapshot":'
            return self._write(h, 200, data, length=len(data) + 100)
        # oversize / oversize_chunked: 64 MiB + 1 enviados até o cliente desistir.
        self._finish(call, "network", status=200, programmed=True)
        h.close_connection = True
        total = MAX_RESPONSE_BYTES + 1
        block = b"a" * (1 << 16)
        try:
            h.send_response(200)
            h.send_header("Content-Type", "application/json; charset=utf-8")
            if kind == "oversize":
                h.send_header("Content-Length", str(total))
            else:
                h.send_header("Transfer-Encoding", "chunked")
            h.send_header("Connection", "close")
            h.end_headers()
            sent = 0
            while sent < total:
                piece = block[:min(len(block), total - sent)]
                if kind == "oversize":
                    h.wfile.write(piece)
                else:
                    h.wfile.write(f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n")
                sent += len(piece)
                with self._cond:
                    if call.gen != self._gen or self._stopped:
                        return
            if kind == "oversize_chunked":
                h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()
        except OSError:
            return


# ---------------------------------------------------------------------------------------------
# O dublê da sessão
# ---------------------------------------------------------------------------------------------

_SESSION: FakeEngine | None = None
_SESSION_LOCK = threading.Lock()


def session_engine() -> FakeEngine:
    """O motor falso único da sessão de testes (criado na 1ª chamada, fechado na saída do processo).

    A fixture `engine_fake` e os helpers sem ela (ex.: a semente de disparos) usam este mesmo
    servidor; a fixture faz `reset(repos_root=…)` a cada teste.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None or _SESSION.closed:
            _SESSION = FakeEngine()
            atexit.register(_SESSION.close)
        return _SESSION


__all__ = [
    "ANSWER_SENTINEL", "ENGINE_VERSION", "ERROR_MESSAGES", "ERROR_STATUS", "FIELD_TYPES", "IDENTITY_FREE_PRESETS",
    "MAX_BODY_BYTES", "MAX_RESPONSE_BYTES", "NETWORK_KINDS", "OPS", "PL_COMMIT", "PRESETS", "SENTINEL_KEY",
    "TECHNICAL_DETAIL", "TECHNICAL_DETAIL_SENTINEL", "EngineCall", "FakeBroken", "FakeEngine", "FakeField",
    "FakeFormatError", "FakeMedia", "FakeOption", "FakeQuestion", "FakeReason", "FakeWarning", "IssuedVariant",
    "Reply", "build_variant", "grade_answers", "html_to_text", "png_bytes", "preset", "raw_shape_problems",
    "repo_key", "schema_errors", "session_engine",
]
