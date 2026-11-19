"""CLI `cq students import` e `cq students summary` (o vínculo do aluno).

Cobre a importação da lista e o resumo da adesão pelo CLI: os
comandos, o arquivo da lista (código 2, nenhuma chamada à API e a mensagem exata, sem caminho nem
conteúdo), o texto para pessoas, os erros (`Estado atual:` no `invalid_state`; cada `rows[i]` como
`  - linha <N>: <reason>`, com N a linha do arquivo em que o registro começa) e o `--json` (o corpo da
API, sem tradução).

O CLI roda em processo pelo `cli_invoke` do conftest (`CliRunner` com o `TestClient` da API em
`obj["http"]`), como nos outros testes do CLI. Os pedidos que o CLI faz são registrados pelo gancho de pedido do
`TestClient` (o corpo enviado é comparado com as linhas do arquivo, sem aparar). Os arquivos da lista
são escritos em bytes em `tmp_path`, para fixar cada linha física. Identificadores, pseudônimos e
cursores nunca são literais: vêm da própria API. Horários pelo `FakeClock` e `iso(dt)`.
"""

from __future__ import annotations

import contextlib
import itertools
import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    create_token,
    import_students,
    register_bot,
    register_person,
    resend_invites,
    students_summary,
    write_roster,
)
from tests.helpers.canary import STUDENT_A, STUDENT_B, STUDENT_PREFIXES, STUDENT_TARGETS, find_canary
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_error, assert_matches

ADMIN = auth(ADMIN_TOKEN)
FOUR_MIB = 4 * 1024 * 1024  # teto do arquivo da lista
HEADER_REASON = ("a primeira linha do arquivo da lista deve ter exatamente as colunas ra, nome e email, "
                 "separadas por vírgula ou por ponto e vírgula.")

# Os rótulos dos alvos da **lista** dos alunos canário (nome, palavras, RA, e-mail, parte local e
# domínio); os alvos `aluno-tg-*` são das contas do Telegram e não estão no arquivo.
LIST_LABELS = sorted(label for label, _ in STUDENT_TARGETS if not label.startswith("aluno-tg-"))


# --------------------------------------------------------------------------------------------------
# Apoio


def _out(*lines: str) -> str:
    """Saída esperada: uma linha por item, cada uma terminada por quebra de linha."""
    return "".join(f"{line}\n" for line in lines)


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"exceção não tratada no CLI: {result.exception!r}")
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _ok(result: Any) -> None:
    _no_traceback(result)
    assert result.exit_code == 0, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stderr == ""


def _failed(result: Any, code: int) -> None:
    _no_traceback(result)
    assert result.exit_code == code, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""


@contextlib.contextmanager
def _recorded_requests(client: Any) -> Iterator[list[Any]]:
    """Registra cada pedido que passa pelo `TestClient` (gancho de pedido do httpx2)."""
    seen: list[Any] = []
    hooks = client.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


def _sent_import_body(seen: list[Any], code: str) -> Any:
    """O CLI fez exatamente um pedido, o `POST /v1/offerings/{ref}/students`; devolve o corpo JSON."""
    assert [(r.method, r.url.path) for r in seen] == [("POST", f"/v1/offerings/{code}/students")]
    return json.loads(seen[0].content)


class _Response:
    """Resposta HTTP mínima (status e corpo JSON), como a do `test_cli_team.py`."""

    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


def _write(tmp_path: Any, name: str, content: bytes) -> Any:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _import_args(ref: str, path: Any) -> list[str]:
    return ["students", "import", ref, "--file", str(path)]


def _ref(offering: dict) -> dict:
    """O `OfferingRef` do contrato (o `offering_id` e o `code`)."""
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _people(client: Any) -> list[dict]:
    resp = client.get("/v1/people", headers=ADMIN)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("people.list", 200, body)
    return body["items"]


def _enrollments(sql: Any, code: str) -> list[tuple]:
    """(pseudo_id, role, state) das matrículas do oferecimento, em ordem de pseudônimo."""
    return sql.execute(
        "SELECT p.pseudo_id::text, e.role, e.state FROM enrollments e "
        "JOIN offerings o ON o.id = e.offering_id JOIN people p ON p.id = e.person_id "
        "WHERE o.code = %s ORDER BY p.pseudo_id", (code,)).fetchall()


def _events(client: Any, *types: str) -> list[dict]:
    """Os eventos dos tipos dados, em ordem de `seq` (crescente), sem o `seq`."""
    events = all_events(client, ADMIN_TOKEN, *types)
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _imported_event(body: dict, at: str) -> dict:
    """`student.list_imported` com as contagens da resposta, pelo token inicial."""
    return {"type": "student.list_imported", "occurred_at": at, "actor_token_id": "bootstrap",
            "data": {"offering_id": body["offering"]["offering_id"], "received": body["received"],
                     "people_created": body["people_created"], "people_reused": body["people_reused"],
                     "enrollments_created": body["enrollments_created"],
                     "already_enrolled": body["already_enrolled"]}}


def _import_line(body: dict) -> str:
    """a linha de `students import` (o `body` esperado é escrito com valores literais)."""
    return (f"Lista importada em {body['offering']['code']}: {body['received']} linhas; "
            f"{body['people_created']} pessoas novas, {body['people_reused']} já cadastradas; "
            f"{body['enrollments_created']} matrículas novas, {body['already_enrolled']} já matriculados.")


def _assert_import_output(result: Any, json_mode: bool, expected: dict) -> None:
    _ok(result)
    if json_mode:
        body = json.loads(result.stdout)
        assert_matches("students.import", 200, body)
        assert body == expected
    else:
        assert result.stdout == _out(_import_line(expected))


def _file_values(rows: list[dict]) -> list[str]:
    """Os valores do arquivo (aparados), a parte local e o domínio de cada e-mail."""
    values: list[str] = []
    for row in rows:
        for key in ("name", "ra", "email"):
            if row[key].strip():
                values.append(row[key].strip())
        email = row["email"].strip()
        if "@" in email:
            values.extend(part for part in email.split("@", 1) if part)
    return values


def _assert_no_file_data(result: Any, rows: list[dict]) -> None:
    """Nenhum valor do arquivo aparece no stdout nem no stderr."""
    text = (result.stdout + "\n" + result.stderr).casefold()
    for value in _file_values(rows):
        assert value.casefold() not in text, f"dado do arquivo da lista na saída do CLI: {value!r}"


def _active_offering(client: Any, bot_api: Any, clock: Any, code: str) -> tuple[dict, int]:
    """Um oferecimento ativo, com bot registrado na Bot API falsa; devolve o `Offering` e o id do bot."""
    offering = create_offering(client, ADMIN_TOKEN, code=code)
    telegram_bot_id = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(telegram_bot_id))
    clock.advance(5)
    active = activate_bot(client, ADMIN_TOKEN, code)
    assert (active["offering_id"], active["state"]) == (offering["offering_id"], "active")
    return active, telegram_bot_id


def _post_ok(client: Any, op_id: str, path: str, body: Any = None) -> dict:
    resp = client.post(path, headers=ADMIN, **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    out = resp.json()
    assert_matches(op_id, 200, out)
    return out


def _close(client: Any, code: str) -> dict:
    closed = _post_ok(client, "offerings.close", f"/v1/offerings/{code}/close")
    assert closed["state"] == "closed"
    return closed


def _archive(client: Any, code: str) -> dict:
    prepared = _post_ok(client, "offerings.prepare_archive", f"/v1/offerings/{code}/archive/prepare")
    archived = _post_ok(client, "offerings.archive", f"/v1/offerings/{code}/archive",
                        {"confirmation_code": prepared["confirmation_code"]})
    assert archived["state"] == "archived"
    return archived


# --------------------------------------------------------------------------------------------------
# students import: arquivo válido (texto e --json)

# 1ª importação: vírgula, fim de linha LF, sem BOM, uma pessoa nova.
FIRST_FILE = b"ra,nome,email\n5510000101,Aurora Primeira Leme,aurora.leme@turma.example.org\n"
FIRST_ROWS = [{"ra": "5510000101", "name": "Aurora Primeira Leme", "email": "aurora.leme@turma.example.org"}]

# 2ª importação: BOM, ponto e vírgula, cabeçalho em outra ordem e caixa, CRLF, os registros com todos os
# campos vazios ignorados (linha vazia, `;;` e `""`), campo entre aspas com o separador, aspas
# duplicadas e espaços nas pontas (repassados sem aparar).
SECOND_FILE = (
    "\ufeffEmail;ra;NOME\r\n"
    " AURORA.Leme@Turma.Example.org ;5510000101;Aurora Primeira Leme\r\n"
    "\r\n"
    "bruno.previo@turma.example.org;5510000102;  Bruno  Outro Nome \r\n"
    ";;\r\n"
    'carla.nova@turma.example.org;5510000103;"Nova; Carla Inventada"\r\n'
    '""\r\n'
    "davi.novo@turma.example.org ;5510000104;Davi Novo Pires\r\n"
    'elisa.nova@turma.example.org;5510000105;"Elisa ""Lisa"" Nova"\r\n'
).encode("utf-8")
SECOND_ROWS = [
    {"ra": "5510000101", "name": "Aurora Primeira Leme", "email": " AURORA.Leme@Turma.Example.org "},
    {"ra": "5510000102", "name": "  Bruno  Outro Nome ", "email": "bruno.previo@turma.example.org"},
    {"ra": "5510000103", "name": "Nova; Carla Inventada", "email": "carla.nova@turma.example.org"},
    {"ra": "5510000104", "name": "Davi Novo Pires", "email": "davi.novo@turma.example.org "},
    {"ra": "5510000105", "name": 'Elisa "Lisa" Nova', "email": "elisa.nova@turma.example.org"},
]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_import_valid_file_text_and_json(json_mode: bool, cli_invoke: Any, client: Any, clock: Any, sql: Any,
                                         tmp_path: Any) -> None:
    """Casos de uso da importação; aluno já matriculado; as saídas do CLI
    (arquivo da lista, texto para pessoas, `--json`): `cq students import REF --file CAMINHO` lê o
    arquivo (BOM, `,` ou `;` pelo cabeçalho, colunas em qualquer ordem e caixa, CRLF, linha vazia, `;;`
    e `""` ignorados, aspas do CSV padrão) e manda à API exatamente as linhas do arquivo, sem aparar, num único
    `POST`; a saída é `Lista importada em <code>: <received> linhas; <people_created> pessoas novas,
    <people_reused> já cadastradas; <enrollments_created> matrículas novas, <already_enrolled> já
    matriculados.` (texto) ou o corpo da API (`--json`), com as contagens exatas (2ª importação:
    5 recebidas, 3 novas, 2 reaproveitadas, 4 matrículas novas e 1 já matriculado); todos ficam
    matriculados como `student`/`pending`; um `student.list_imported` por importação; nenhum dado do
    arquivo nem pseudônimo na saída."""
    offering = create_offering(client, ADMIN_TOKEN, code="mc102-lista-cli")
    code = offering["code"]
    reused = register_person(client, ADMIN_TOKEN, name="Bruno Cadastrado Prévio", ra="5510000102",
                             email="bruno.previo@turma.example.org")

    clock.advance(60)
    first_at = iso(clock.now())
    first_path = _write(tmp_path, "lista-1.csv", FIRST_FILE)
    with _recorded_requests(client) as seen:
        first = cli_invoke(_import_args(code, first_path), json=json_mode)
    first_body = {"offering": _ref(offering), "received": 1, "people_created": 1, "people_reused": 0,
                  "enrollments_created": 1, "already_enrolled": 0}
    _assert_import_output(first, json_mode, first_body)
    assert _sent_import_body(seen, code) == {"rows": FIRST_ROWS}

    clock.advance(60)
    second_at = iso(clock.now())
    second_path = _write(tmp_path, "lista-2.csv", SECOND_FILE)
    with _recorded_requests(client) as seen:
        second = cli_invoke(_import_args(code, second_path), json=json_mode)
    second_body = {"offering": _ref(offering), "received": 5, "people_created": 3, "people_reused": 2,
                   "enrollments_created": 4, "already_enrolled": 1}
    _assert_import_output(second, json_mode, second_body)
    assert _sent_import_body(seen, code) == {"rows": SECOND_ROWS}

    people = _people(client)
    assert len(people) == 5 and people[0] == reused
    assert _enrollments(sql, code) == sorted((p["pseudo_id"], "student", "pending") for p in people)
    assert _events(client, "student.list_imported") == [_imported_event(first_body, first_at),
                                                       _imported_event(second_body, second_at)]
    for result, rows in ((first, FIRST_ROWS), (second, SECOND_ROWS)):
        _assert_no_file_data(result, rows)
        for person in people:
            assert person["pseudo_id"] not in result.stdout + result.stderr


# --------------------------------------------------------------------------------------------------
# students import: arquivo fora do formato (código 2, nenhuma chamada à API)

_ROW_A = b"5510000401,Aline Sentinela Quaresma,aline.sentinela@turma.example.org\n"
_ROW_CP1252 = "5510000402,José Sentinela Varela,jose.sentinela@turma.example.org\n".encode("cp1252")


def _big_file() -> bytes:
    """Um arquivo com mais de 4 MiB (cabeçalho válido e linhas válidas)."""
    row = b"5510000403,Grande Sentinela Lima,grande.sentinela@turma.example.org\n"
    return b"ra,nome,email\n" + row * (FOUR_MIB // len(row) + 1)


def _field_above_limit_file() -> bytes:
    """Um nome com mais de 131.072 caracteres (o teto de campo do `csv`) no registro da linha 3."""
    return (b"ra,nome,email\n" + _ROW_A + b"5510000402," + b"x" * (131_072 + 10)
            + b",campo.sentinela@turma.example.org\n")


def _too_many_rows_file() -> bytes:
    """1.001 linhas de alunos válidas e distintas (uma além do teto do CLI)."""
    rows = "".join(f"{5540000000 + i},Aluno Sentinela Numero {i},aluno{i}.sentinela@turma.example.org\n"
                   for i in range(1001))
    return ("ra,nome,email\n" + rows).encode("utf-8")


_MISSING_FILE = object()
_DIRECTORY = object()

_READER_CASES: list[tuple[str, Any, str]] = [
    ("inexistente", _MISSING_FILE, "não foi possível ler o arquivo da lista."),
    ("diretorio", _DIRECTORY, "não foi possível ler o arquivo da lista."),
    ("acima-de-4-mib", _big_file, "o arquivo da lista passa de 4 MiB."),
    ("cp1252-na-linha-3", lambda: b"ra,nome,email\n" + _ROW_A + _ROW_CP1252,
     "o arquivo da lista não está em UTF-8 (linha 3)."),
    ("utf-16", lambda: ("ra,nome,email\n" + _ROW_A.decode()).encode("utf-16"),
     "o arquivo da lista não está em UTF-8 (linha 1)."),
    ("cabecalho-com-espacos", lambda: b"ra, nome, email\n" + _ROW_A, HEADER_REASON),
    ("cabecalho-coluna-repetida", lambda: b"ra,ra,email\n" + _ROW_A, HEADER_REASON),
    ("cabecalho-coluna-extra", lambda: b"ra,nome,email,\n" + _ROW_A, HEADER_REASON),
    ("cabecalho-outro-nome", lambda: b"E-mail;ra;nome\n" + _ROW_A.replace(b",", b";"), HEADER_REASON),
    ("arquivo-vazio", lambda: b"", HEADER_REASON),
    ("cabecalho-depois-de-linha-vazia", lambda: b"\nra,nome,email\n" + _ROW_A, HEADER_REASON),
    ("aspas-seguidas-de-texto",
     lambda: b"ra,nome,email\n" + _ROW_A + b'5510000402,"Bento"x,bento.sentinela@turma.example.org\n',
     "o arquivo da lista não é um CSV válido na linha 3."),
    ("aspas-sem-fechamento",
     lambda: b"ra,nome,email\n" + _ROW_A + b'5510000402,"Bento Sentinela,bento.sentinela@turma.example.org\n',
     "o arquivo da lista não é um CSV válido na linha 3."),
    ("campo-acima-de-131072-caracteres", _field_above_limit_file,
     "o arquivo da lista não é um CSV válido na linha 3."),
    ("duas-colunas-depois-de-linha-vazia",
     lambda: b"ra,nome,email\n" + _ROW_A + b"\n5510000402,Bento Sentinela Varela\n",
     "a linha 4 do arquivo da lista não tem as 3 colunas do cabeçalho."),
    ("virgulas-num-arquivo-com-ponto-e-virgula",
     lambda: b"ra;nome;email\n" + _ROW_A.replace(b",", b";") + b",,\n",
     "a linha 3 do arquivo da lista não tem as 3 colunas do cabeçalho."),
    ("ponto-e-virgula-num-arquivo-com-virgula", lambda: b"ra,nome,email\n" + _ROW_A + b";;\n",
     "a linha 3 do arquivo da lista não tem as 3 colunas do cabeçalho."),
    ("so-cabecalho-e-linhas-vazias", lambda: b'ra,nome,email\r\n\r\n,,\r\n""\r\n',
     "o arquivo da lista não tem nenhuma linha de aluno."),
    ("so-cabecalho-e-linhas-vazias-ponto-e-virgula", lambda: b'ra;nome;email\n;;\n\n""\n',
     "o arquivo da lista não tem nenhuma linha de aluno."),
    ("mais-de-1000-linhas", _too_many_rows_file, "o arquivo da lista tem mais de 1.000 linhas de alunos."),
]


@pytest.mark.parametrize(("content", "reason"), [(c, r) for _, c, r in _READER_CASES],
                         ids=[case[0] for case in _READER_CASES])
def test_import_file_out_of_format_is_usage_error_without_request(content: Any, reason: str, cli_invoke: Any,
                                                                  client: Any, sql: Any, tmp_path: Any) -> None:
    """Regra (um arquivo fora do formato é recusado pelo CLI como erro de uso, antes de chamar a
    API e sem repetir o conteúdo, inclusive o cabeçalho com espaço ou precedido de linha vazia; `;;` num
    arquivo com vírgula e `,,` num arquivo com ponto e vírgula são um campo não vazio; `,,`, `;;` e `""`
    no próprio separador são ignorados); o arquivo da lista (cada motivo de recusa,
    inclusive o campo acima de 131.072 caracteres): código 2,
    stdout vazio, `Uso incorreto: <motivo>` exato no stderr (também com `--json`), sem o caminho nem
    nenhum valor do arquivo, e **zero** pedidos à API; nada é gravado no oferecimento."""
    offering = create_offering(client, ADMIN_TOKEN, code="mc102-lista-uso")
    if content is _MISSING_FILE:
        path = tmp_path / "lista-inexistente.csv"
    elif content is _DIRECTORY:
        path = tmp_path / "pasta-da-lista"
        path.mkdir()
    else:
        path = _write(tmp_path, "lista-fora-do-formato.csv", content())

    for json_mode in (False, True):
        with _recorded_requests(client) as seen:
            result = cli_invoke(_import_args(offering["code"], path), json=json_mode)
        _failed(result, 2)
        assert result.stderr == _out(f"Uso incorreto: {reason}")
        assert seen == [], "nenhum pedido à API com o arquivo fora do formato"
        assert "sentinela" not in result.stderr.casefold()
        assert str(path) not in result.stderr and path.name not in result.stderr
    assert _people(client) == []
    assert _enrollments(sql, offering["code"]) == []


def test_import_option_usage_errors_exit_2_without_request(cli_invoke: Any, client: Any, tmp_path: Any) -> None:
    """Os comandos do CLI (`cq students import REF --file CAMINHO`, `--file` obrigatório e
    texto simples) e a regra geral do CLI (uso incorreto sem eco): sem `--file`, sem o valor de `--file`, sem `REF`
    e com argumento a mais → código 2, a mensagem exata e nenhum pedido à API; na mesma função,
    `cq students import --help` sai com 0 e cita `--file`."""
    offering = create_offering(client, ADMIN_TOKEN, code="mc102-lista-opcoes")
    path = write_roster(tmp_path / "lista.csv", FIRST_ROWS)
    cases = [
        (["students", "import", offering["code"]], "falta a opção --file."),
        (["students", "import", offering["code"], "--file"], "falta o valor da opção --file."),
        (["students", "import", "--file", str(path)], "falta um argumento obrigatório."),
        ([*_import_args(offering["code"], path), "argumentosentinela"], "há argumentos a mais."),
    ]
    for args, message in cases:
        for json_mode in (False, True):
            with _recorded_requests(client) as seen:
                result = cli_invoke(args, json=json_mode)
            _failed(result, 2)
            assert result.stderr == _out(f"Uso incorreto: {message}"), args
            assert seen == [], args
            assert "argumentosentinela" not in result.stderr
    assert _people(client) == []

    help_result = cli_invoke(["students", "import", "--help"])
    _ok(help_result)
    assert "--file" in help_result.stdout


# --------------------------------------------------------------------------------------------------
# students import: erros por linha traduzidos para a linha do arquivo

# Linhas físicas: 1 cabeçalho; 2 Aurora; 3 vazia; 4 Bento (nome vazio); 5-6 Carla (registro de duas
# linhas, válido); 7 e 8 vazias; 9 Davi (RA repetido da linha 2); 10-11 Elisa (registro de duas linhas,
# RA com espaço); 12 Fábio (e-mail mal formado).
FORMAT_ERRORS_FILE = (
    "ra,nome,email\n"
    "5510000201,Aurora Inventada Leme,aurora.leme@turma.example.org\n"
    "\n"
    "5510000202,,bento.brito@turma.example.org\n"
    '5510000203,"Carla Fictícia\nde Souza",carla.souza@turma.example.org\n'
    "\n"
    "\n"
    "5510000201,Davi Imaginário Rocha,davi.rocha@turma.example.org\n"
    '5510 0205,"Elisa\nQuimera Porto",elisa.porto@turma.example.org\n'
    "5510000206,Fábio Sonhado Lima,semarroba-sentinela-turma\n"
).encode("utf-8")
FORMAT_ERRORS_ROWS = [
    {"ra": "5510000201", "name": "Aurora Inventada Leme", "email": "aurora.leme@turma.example.org"},
    {"ra": "5510000202", "name": "", "email": "bento.brito@turma.example.org"},
    {"ra": "5510000203", "name": "Carla Fictícia\nde Souza", "email": "carla.souza@turma.example.org"},
    {"ra": "5510000201", "name": "Davi Imaginário Rocha", "email": "davi.rocha@turma.example.org"},
    {"ra": "5510 0205", "name": "Elisa\nQuimera Porto", "email": "elisa.porto@turma.example.org"},
    {"ra": "5510000206", "name": "Fábio Sonhado Lima", "email": "semarroba-sentinela-turma"},
]
FORMAT_ERRORS_FIELDS = [("rows[1]", "too_short"), ("rows[3]", "duplicate"), ("rows[4]", "invalid_format"),
                        ("rows[5]", "invalid_format")]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_import_row_errors_are_translated_to_file_lines(json_mode: bool, cli_invoke: Any, client: Any,
                                                        sql: Any, tmp_path: Any) -> None:
    """Os erros de formato apontam as linhas pelo número e pelo motivo, sem ecoar
    valores; os erros do CLI (cada `rows[i]` sai como
    `  - linha <N>: <reason>`, com N a linha do arquivo em que o registro começa; `--json`: o corpo da
    API intacto): com linhas vazias e registros de duas linhas, o 400 da API (`rows[1]` nome vazio,
    `rows[3]` RA repetido, `rows[4]` RA com espaço, `rows[5]` e-mail mal formado) sai com código 1,
    `Erro: Pedido inválido.` e `  - linha 4: too_short`, `  - linha 9: duplicate`,
    `  - linha 10: invalid_format` e `  - linha 12: invalid_format`; o corpo enviado é o do arquivo, e
    nada é gravado."""
    offering = create_offering(client, ADMIN_TOKEN, code="mc102-lista-linhas")
    code = offering["code"]
    path = _write(tmp_path, "lista-com-erros.csv", FORMAT_ERRORS_FILE)
    with _recorded_requests(client) as seen:
        result = cli_invoke(_import_args(code, path), json=json_mode)
    _failed(result, 1)
    assert _sent_import_body(seen, code) == {"rows": FORMAT_ERRORS_ROWS}

    api = client.post(f"/v1/offerings/{code}/students", json={"rows": FORMAT_ERRORS_ROWS}, headers=ADMIN)
    assert_error(api, 400, "invalid_request", fields=FORMAT_ERRORS_FIELDS)
    if json_mode:
        assert json.loads(result.stderr) == api.json()
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['invalid_request']}", "  - linha 4: too_short",
                                     "  - linha 9: duplicate", "  - linha 10: invalid_format",
                                     "  - linha 12: invalid_format")
    _assert_no_file_data(result, FORMAT_ERRORS_ROWS)
    assert "5510 0205" not in result.stderr
    assert _people(client) == []
    assert _enrollments(sql, code) == []
    assert _events(client, "student.list_imported") == []


# Linhas físicas: 1 cabeçalho (`;`, caixa mista); 2 Gustavo (novo); 3 vazia; 4-5 Helena (registro de
# duas linhas com CRLF dentro das aspas; RA de uma pessoa cadastrada com outro e-mail); 6 Igor (e-mail
# de uma pessoa cadastrada sem RA).
CONFLICT_FILE = (
    "RA;Nome;Email\r\n"
    "5510000301;Gustavo Novo Tavares;gustavo.tavares@turma.example.org\r\n"
    "\r\n"
    '5510000302;"Helena\r\nOutro Sobrenome";helena.outra@turma.example.org\r\n'
    "5510000303;Igor Qualquer Nunes;IVO.Cadastrado@Turma.Example.org\r\n"
).encode("utf-8")
CONFLICT_ROWS = [
    {"ra": "5510000301", "name": "Gustavo Novo Tavares", "email": "gustavo.tavares@turma.example.org"},
    {"ra": "5510000302", "name": "Helena\r\nOutro Sobrenome", "email": "helena.outra@turma.example.org"},
    {"ra": "5510000303", "name": "Igor Qualquer Nunes", "email": "IVO.Cadastrado@Turma.Example.org"},
]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_import_list_conflict_is_translated_to_file_lines(json_mode: bool, cli_invoke: Any, client: Any,
                                                          sql: Any, tmp_path: Any) -> None:
    """Conflitos com pessoas cadastradas (409), apontados pela linha e pelo motivo, sem eco;
    pessoa já cadastrada (RA de pessoa com outro e-mail; e-mail de pessoa
    sem RA); o erro `list_conflict` e os erros do CLI: código 1, `Erro: Há linhas da lista em
    conflito com pessoas cadastradas ou com a equipe; nada foi gravado.` e `  - linha 4: duplicate` e
    `  - linha 6: duplicate` (o registro de duas linhas começa na linha 4); `--json`: o corpo da API
    intacto; nada é gravado, e o pseudônimo das pessoas cadastradas não aparece."""
    offering = create_offering(client, ADMIN_TOKEN, code="mc102-lista-conflito")
    code = offering["code"]
    helena = register_person(client, ADMIN_TOKEN, name="Helena Registrada Antes", ra="5510000302",
                             email="helena.registrada@turma.example.org")
    ivo = register_person(client, ADMIN_TOKEN, name="Ivo Cadastrado Ramos", email="ivo.cadastrado@turma.example.org")
    path = _write(tmp_path, "lista-em-conflito.csv", CONFLICT_FILE)
    with _recorded_requests(client) as seen:
        result = cli_invoke(_import_args(code, path), json=json_mode)
    _failed(result, 1)
    assert _sent_import_body(seen, code) == {"rows": CONFLICT_ROWS}

    api = client.post(f"/v1/offerings/{code}/students", json={"rows": CONFLICT_ROWS}, headers=ADMIN)
    assert_error(api, 409, "list_conflict", fields=[("rows[1]", "duplicate"), ("rows[2]", "duplicate")])
    if json_mode:
        assert json.loads(result.stderr) == api.json()
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['list_conflict']}", "  - linha 4: duplicate",
                                     "  - linha 6: duplicate")
    _assert_no_file_data(result, CONFLICT_ROWS)
    for person in (helena, ivo):
        assert person["pseudo_id"] not in result.stderr
    assert _people(client) == [helena, ivo]
    assert _enrollments(sql, code) == []
    assert _events(client, "student.list_imported") == []


class _ForgedImportError:
    """Cliente HTTP que **não** chama a API: registra o pedido e devolve o 400 dado. Serve para amarrar a
    tradução do texto a campos que a API só produz noutros canais (`rows` sem índice, `body`), sem
    depender de montar esses erros pelo CLI."""

    def __init__(self, body: dict) -> None:
        self._body = body
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        # O corpo como JSON (a forma que iria no fio), qualquer que seja o tipo de sequência usado.
        self.calls.append((method, url, json.loads(json.dumps(kwargs.get("json")))))
        return _Response(400, self._body)


# Linhas físicas: 1 cabeçalho; 2-3 Jonas (registro de duas linhas); 4 vazia; 5 Karina.
FORGED_FILE = ('ra,nome,email\n5510000501,"Jonas\nInventado Reis",jonas.reis@turma.example.org\n\n'
               "5510000502,Karina Fictícia Luz,karina.luz@turma.example.org\n").encode("utf-8")
FORGED_ROWS = [
    {"ra": "5510000501", "name": "Jonas\nInventado Reis", "email": "jonas.reis@turma.example.org"},
    {"ra": "5510000502", "name": "Karina Fictícia Luz", "email": "karina.luz@turma.example.org"},
]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_import_other_error_fields_keep_the_base_form(json_mode: bool, cli_invoke: Any, tmp_path: Any) -> None:
    """Os erros do CLI (só os itens `rows[i]` saem como `  - linha <N>: <reason>`, com N a
    linha em que o registro começa; os demais campos, como nos outros comandos, `  - <field>: <reason>`; `--json`: o
    corpo da API, sem tradução): com a resposta 400 forjada, `rows: too_many` sai como `  - rows:
    too_many`, e `rows[1]` seguido de `body: unknown_field` sai como `  - linha 5: invalid_format` e
    `  - body: unknown_field` (o registro de duas linhas e a linha vazia deslocam a contagem); em todos,
    código 1, um único `POST` com as linhas do arquivo, e nenhum dado do arquivo na saída."""
    code = "mc102-lista-forjada"
    path = _write(tmp_path, "lista-forjada.csv", FORGED_FILE)
    cases = [
        ([("rows", "too_many")], ["  - rows: too_many"]),
        ([("rows[1]", "invalid_format"), ("body", "unknown_field")],
         ["  - linha 5: invalid_format", "  - body: unknown_field"]),
    ]
    for fields, lines in cases:
        body = {"error": {"code": "invalid_request", "message": MESSAGES["invalid_request"],
                          "fields": [{"field": f, "reason": r} for f, r in fields]}}
        http = _ForgedImportError(body)
        result = cli_invoke(_import_args(code, path), json=json_mode, http=http)
        _failed(result, 1)
        assert http.calls == [("POST", f"http://testserver/v1/offerings/{code}/students", {"rows": FORGED_ROWS})]
        if json_mode:
            assert json.loads(result.stderr) == body
        else:
            assert result.stderr == _out(f"Erro: {MESSAGES['invalid_request']}", *lines)
        _assert_no_file_data(result, FORGED_ROWS)


# --------------------------------------------------------------------------------------------------
# students import: erros da API sem linhas (estado, inexistente, permissão)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_import_api_errors_report_state_and_permission(json_mode: bool, cli_invoke: Any, client: Any,
                                                       clock: Any, sql: Any, bot_api: Any, tmp_path: Any) -> None:
    """A importação e os erros do CLI, como nos outros comandos: importar num oferecimento
    encerrado ou arquivado → código 1, `Erro: A operação não é permitida no estado atual do
    oferecimento.` e `Estado atual: closed|archived`; oferecimento inexistente → `Erro: Recurso não
    encontrado.`; token sem `people:write` → `Erro: Este token não tem a permissão necessária.` e
    `Permissão necessária: people:write`; com `--json`, o corpo de erro da API; em todos, o CLI mandou
    as linhas do arquivo num único pedido, e nada é gravado."""
    closed, _ = _active_offering(client, bot_api, clock, "mc102-lista-encerrada")
    _close(client, closed["code"])
    archived = create_offering(client, ADMIN_TOKEN, code="mc102-lista-arquivada")
    _archive(client, archived["code"])
    draft = create_offering(client, ADMIN_TOKEN, code="mc102-lista-permissao")
    reader, _ = create_token(client, ADMIN_TOKEN, ["people:read", "offerings:read"], label="sem escrita")
    path = _write(tmp_path, "lista.csv", FIRST_FILE)

    cases = [
        (closed["code"], ADMIN_TOKEN,
         {"error": {"code": "invalid_state", "message": MESSAGES["invalid_state"], "state": "closed"}},
         [f"Erro: {MESSAGES['invalid_state']}", "Estado atual: closed"]),
        (archived["code"], ADMIN_TOKEN,
         {"error": {"code": "invalid_state", "message": MESSAGES["invalid_state"], "state": "archived"}},
         [f"Erro: {MESSAGES['invalid_state']}", "Estado atual: archived"]),
        ("codigo-inexistente", ADMIN_TOKEN,
         {"error": {"code": "not_found", "message": MESSAGES["not_found"]}},
         [f"Erro: {MESSAGES['not_found']}"]),
        (draft["code"], reader,
         {"error": {"code": "forbidden", "message": MESSAGES["forbidden"], "permission": "people:write"}},
         [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: people:write"]),
    ]
    for ref, token, body, lines in cases:
        with _recorded_requests(client) as seen:
            result = cli_invoke(_import_args(ref, path), json=json_mode, token=token)
        _failed(result, 1)
        assert _sent_import_body(seen, ref) == {"rows": FIRST_ROWS}
        if json_mode:
            assert json.loads(result.stderr) == body, ref
        else:
            assert result.stderr == _out(*lines), ref
        _assert_no_file_data(result, FIRST_ROWS)
    assert _people(client) == []
    for offering in (closed, archived, draft):
        assert _enrollments(sql, offering["code"]) == []
    assert _events(client, "student.list_imported") == []


# --------------------------------------------------------------------------------------------------
# students import: nenhum dado do arquivo nas saídas (alunos canário)


def test_import_outputs_carry_no_file_data(cli_invoke: Any, client: Any, tmp_path: Any) -> None:
    """Sem repetir o conteúdo do arquivo e só com contagens, sem ligar linha a pseudônimo;
    a regra da importação (nenhuma resposta, erro ou log traz nome, RA, e-mail ou pseudônimo) e a do CLI:
    com os alunos canário do vínculo no arquivo, as saídas do CLI (stdout e stderr, em texto e `--json`) da
    importação aceita, da repetida, do 400 por repetição, do 409 por conflito, do `invalid_state` e do
    erro de uso não têm nenhum alvo de `STUDENT_PREFIXES` nem os pseudônimos criados; controle positivo:
    o detector acha no próprio arquivo exatamente os alvos da lista dos dois alunos."""
    offering = create_offering(client, ADMIN_TOKEN, code="mc102-lista-canario")
    code = offering["code"]
    archived = create_offering(client, ADMIN_TOKEN, code="mc102-canario-arquivado")
    _archive(client, archived["code"])
    rows = [STUDENT_A.row(), STUDENT_B.row()]
    good = write_roster(tmp_path / "lista-canario.csv", rows)
    repeated = write_roster(tmp_path / "lista-repetida.csv", [*rows, STUDENT_A.row()])
    conflict = write_roster(tmp_path / "lista-conflito.csv",
                            [{"name": STUDENT_A.name, "ra": STUDENT_A.ra, "email": STUDENT_B.email}])
    four_columns = write_roster(tmp_path / "lista-quatro-colunas.csv",
                                [[STUDENT_A.ra, STUDENT_A.name, STUDENT_A.email, STUDENT_B.name]])

    outputs: list[str] = []
    imported = cli_invoke(_import_args(code, good))
    _ok(imported)
    assert imported.stdout == _out(_import_line({
        "offering": _ref(offering), "received": 2, "people_created": 2, "people_reused": 0,
        "enrollments_created": 2, "already_enrolled": 0}))
    outputs.append(imported.stdout + imported.stderr)
    again = cli_invoke(_import_args(code, good), json=True)
    _ok(again)
    assert json.loads(again.stdout) == {"offering": _ref(offering), "received": 2, "people_created": 0,
                                        "people_reused": 2, "enrollments_created": 0, "already_enrolled": 2}
    outputs.append(again.stdout + again.stderr)
    for path, status_code, ref in ((repeated, 1, code), (conflict, 1, code), (good, 1, archived["code"]),
                                   (four_columns, 2, code)):
        for json_mode in (False, True):
            result = cli_invoke(_import_args(ref, path), json=json_mode)
            _failed(result, status_code)
            outputs.append(result.stdout + result.stderr)

    assert find_canary(good.read_text(encoding="utf-8"), prefixes=STUDENT_PREFIXES) == LIST_LABELS
    people = _people(client)
    assert len(people) == 2
    for text in outputs:
        assert find_canary(text, prefixes=STUDENT_PREFIXES) == []
        for person in people:
            assert person["pseudo_id"] not in text


# --------------------------------------------------------------------------------------------------
# students summary


def _summary_body(offering: dict, **counts: int) -> dict:
    """O corpo exato de `students.summary`, com zero nos estados não dados."""
    states = ("no_invite", "sending", "invite_valid", "terms_presented", "invite_lapsed", "send_failed",
              "linked")
    body = {"offering": _ref(offering), "enrolled": counts.pop("enrolled")}
    body.update({state: counts.pop(state, 0) for state in states})
    assert counts == {}, f"estado desconhecido: {counts}"
    return body


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_summary_text_and_json(json_mode: bool, cli_invoke: Any, client: Any, clock: Any, bot_api: Any) -> None:
    """Só contagens, sem identidade nem pseudônimo; a adesão ignora o estado do oferecimento;
    o resumo da adesão e o texto para pessoas do CLI (`Adesão de <code>: <enrolled> alunos
    matriculados.` e, nesta ordem, `  sem convite: <n>`, `  envio em andamento: <n>`, `  convite
    válido: <n>`, `  termo apresentado: <n>`, `  convite vencido ou revogado: <n>`, `  falha no envio:
    <n>`, `  vinculados: <n>`; `--json`: o corpo da API): num oferecimento ativo com 3 alunos
    importados e um reenvio pendente (sem worker), 3 matriculados, 2 sem convite e 1 com envio em
    andamento, pelo código e pelo identificador; o mesmo depois de encerrar o oferecimento; num
    rascunho sem alunos, tudo zero."""
    offering, _ = _active_offering(client, bot_api, clock, "mc102-adesao-cli")
    code = offering["code"]
    draft = create_offering(client, ADMIN_TOKEN, code="mc102-adesao-vazia")
    people = [register_person(client, ADMIN_TOKEN, name=name, ra=ra, email=email) for name, ra, email in (
        ("Aluna Adesão Primeira", "5520000101", "adesao.primeira@turma.example.org"),
        ("Aluno Adesão Segundo", "5520000102", "adesao.segundo@turma.example.org"),
        ("Aluna Adesão Terceira", "5520000103", "adesao.terceira@turma.example.org"))]

    def check(ref: str, body: dict, text: str) -> None:
        result = cli_invoke(["students", "summary", ref], json=json_mode)
        _ok(result)
        if json_mode:
            out = json.loads(result.stdout)
            assert_matches("students.summary", 200, out)
            assert out == body
        else:
            assert result.stdout == text
        assert students_summary(client, ADMIN_TOKEN, ref) == body
        for person in people:
            assert person["pseudo_id"] not in result.stdout

    empty = _summary_body(draft, enrolled=0)
    empty_text = _out(f"Adesão de {draft['code']}: 0 alunos matriculados.", "  sem convite: 0",
                      "  envio em andamento: 0", "  convite válido: 0", "  termo apresentado: 0",
                      "  convite vencido ou revogado: 0", "  falha no envio: 0", "  vinculados: 0")
    check(draft["code"], empty, empty_text)

    imported = import_students(client, ADMIN_TOKEN, code, [
        {"name": "Aluna Adesão Primeira", "ra": "5520000101", "email": "adesao.primeira@turma.example.org"},
        {"name": "Aluno Adesão Segundo", "ra": "5520000102", "email": "adesao.segundo@turma.example.org"},
        {"name": "Aluna Adesão Terceira", "ra": "5520000103", "email": "adesao.terceira@turma.example.org"}])
    assert imported == {"offering": _ref(offering), "received": 3, "people_created": 0, "people_reused": 3,
                        "enrollments_created": 3, "already_enrolled": 0}
    resent = resend_invites(client, ADMIN_TOKEN, code, [people[0]["pseudo_id"]])
    assert resent == {"offering": _ref(offering), "queued": 1, "valid_hours": 168, "not_students": [],
                      "not_found_count": 0}

    expected = _summary_body(offering, enrolled=3, no_invite=2, sending=1)
    expected_text = _out(f"Adesão de {code}: 3 alunos matriculados.", "  sem convite: 2",
                         "  envio em andamento: 1", "  convite válido: 0", "  termo apresentado: 0",
                         "  convite vencido ou revogado: 0", "  falha no envio: 0", "  vinculados: 0")
    check(code, expected, expected_text)
    check(offering["offering_id"], expected, expected_text)
    _close(client, code)
    check(code, expected, expected_text)


class _SummaryWithCounts:
    """Cliente HTTP que repassa ao `TestClient` e, na resposta 200 de `students.summary`, troca as
    contagens por valores distintos (o corpo continua válido no esquema congelado), para que cada linha
    do texto seja amarrada ao seu campo. Guarda os corpos reais recebidos em `real`."""

    def __init__(self, client: Any, counts: dict[str, int]) -> None:
        self._client = client
        self._counts = counts
        self.real: list[dict] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self._client.request(method, url, **kwargs)
        if method == "GET" and url.endswith("/students/summary") and response.status_code == 200:
            body = response.json()
            self.real.append(body)
            forged = {**body, **self._counts}
            assert_matches("students.summary", 200, forged)
            return _Response(200, forged)
        return response


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_summary_text_maps_each_count_to_its_line(json_mode: bool, cli_invoke: Any, client: Any) -> None:
    """O CLI (texto para pessoas de `students summary`, a ordem e o rótulo de cada
    estado; `--json`: o corpo da API sem tradução): com a resposta da API trocada por contagens
    distintas (1 a 7, 28 matriculados), cada linha do texto traz o número do seu campo; com `--json`, a
    saída é o corpo recebido; o CLI fez um único `GET` de `students.summary`, cujo corpo real (rascunho
    sem alunos) é todo zero."""
    draft = create_offering(client, ADMIN_TOKEN, code="mc102-adesao-rotulos")
    counts = {"enrolled": 28, "no_invite": 1, "sending": 2, "invite_valid": 3, "terms_presented": 4,
              "invite_lapsed": 5, "send_failed": 6, "linked": 7}
    http = _SummaryWithCounts(client, counts)
    result = cli_invoke(["students", "summary", draft["code"]], json=json_mode, http=http)
    _ok(result)
    assert http.real == [_summary_body(draft, enrolled=0)]
    if json_mode:
        assert json.loads(result.stdout) == {"offering": _ref(draft), **counts}
    else:
        assert result.stdout == _out(f"Adesão de {draft['code']}: 28 alunos matriculados.",
                                     "  sem convite: 1", "  envio em andamento: 2", "  convite válido: 3",
                                     "  termo apresentado: 4", "  convite vencido ou revogado: 5",
                                     "  falha no envio: 6", "  vinculados: 7")


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_summary_errors(json_mode: bool, cli_invoke: Any, client: Any) -> None:
    """Os erros do resumo (400 do `ref`, 403, 404) e os erros do CLI, como nos outros comandos: oferecimento
    inexistente → código 1 e `Erro: Recurso não encontrado.`; `ref` malformado → `Erro: Pedido
    inválido.` e `  - ref: invalid_format`, sem eco; token sem `offerings:read` → `Permissão
    necessária: offerings:read`; com `--json`, o corpo de erro da API; e o uso incorreto (sem `REF`, com
    argumento a mais) sai com código 2, sem pedido à API."""
    draft = create_offering(client, ADMIN_TOKEN, code="mc102-adesao-erros")
    reader, _ = create_token(client, ADMIN_TOKEN, ["people:read"], label="so pessoas")
    cases = [
        ("codigo-inexistente", ADMIN_TOKEN, {"error": {"code": "not_found", "message": MESSAGES["not_found"]}},
         [f"Erro: {MESSAGES['not_found']}"]),
        ("REFSENTINELA", ADMIN_TOKEN,
         {"error": {"code": "invalid_request", "message": MESSAGES["invalid_request"],
                    "fields": [{"field": "ref", "reason": "invalid_format"}]}},
         [f"Erro: {MESSAGES['invalid_request']}", "  - ref: invalid_format"]),
        (draft["code"], reader,
         {"error": {"code": "forbidden", "message": MESSAGES["forbidden"], "permission": "offerings:read"}},
         [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: offerings:read"]),
    ]
    for ref, token, body, lines in cases:
        result = cli_invoke(["students", "summary", ref], json=json_mode, token=token)
        _failed(result, 1)
        if json_mode:
            assert json.loads(result.stderr) == body, ref
        else:
            assert result.stderr == _out(*lines), ref
        assert "refsentinela" not in result.stderr.casefold()

    for args, message in ((["students", "summary"], "falta um argumento obrigatório."),
                          (["students", "summary", draft["code"], "argumentosentinela"], "há argumentos a mais.")):
        with _recorded_requests(client) as seen:
            result = cli_invoke(args, json=json_mode)
        _failed(result, 2)
        assert result.stderr == _out(f"Uso incorreto: {message}")
        assert seen == []
