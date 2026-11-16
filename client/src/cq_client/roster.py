"""Leitura do arquivo da lista da turma (CSV com `ra`, `nome` e `email`), sem passar o conteúdo
ao modelo.

Módulo puro, sem Click, que o `cq students import` e o servidor MCP reaproveitam: o conteúdo do
arquivo nunca passa pelo modelo.

- `parse_roster(raw)` analisa os bytes já lidos (até `MAX_BYTES`): decodifica em `utf-8-sig` estrito,
  escolhe o separador (`,` ou `;`) pelo cabeçalho (`ra`, `nome` e `email`, em qualquer ordem e caixa) e
  devolve um `Roster` com até `MAX_ROWS` linhas de alunos, com os campos **sem aparar** (as regras dos campos
  ficam na API) e, para cada linha, a linha física do arquivo em que ela começa (o cabeçalho é a
  linha 1).
- `read_roster(path)` lê o arquivo em bytes (até `MAX_BYTES + 1`) e chama `parse_roster`; o servidor MCP
  lê o arquivo por conta própria e chama `parse_roster` direto.
- `row_line(field, lines)` traduz um campo `rows[i]` de um erro da API para a linha do arquivo.
- `RosterFileError(reason)`: arquivo fora do formato; `reason` é uma das mensagens fixas de
  leitura da lista, sem caminho nem conteúdo.

Regras (inclusive a dos registros vazios):
- o separador vem só da primeira linha física (até o primeiro `\\r\\n`, `\\r` ou `\\n`), analisada com
  `,` e depois com `;`; vale o que dá exatamente as células `ra`, `nome` e `email` (comparadas com
  `.lower()`, sem aparar, sem repetição nem colunas extras). Nada de `csv.Sniffer`, que é heurístico;
- o leitor é o `csv.reader` do dialeto `excel` com `strict=True` (uma aspa não fechada não engole o
  resto do arquivo em silêncio) sobre `io.StringIO(texto, newline="")` (`\\r\\n`, `\\r` e `\\n` são fim
  de linha, e a quebra dentro de aspas fica no campo);
- depois do cabeçalho, um registro em que todos os campos são vazios é ignorado (`[]` da linha vazia,
  `,,` num arquivo com `,`, `;;` num arquivo com `;` e `""`); o outro separador é conteúdo, então `;;`
  num arquivo com `,` é um registro de um campo só (erro de colunas);
- o limite de campo do `csv` (`field_size_limit`, estado global do módulo) nunca é alterado: o servidor MCP
  é um processo longo. Um campo acima dele é CSV malformado;
- só o primeiro erro estrutural é relatado: depois de um `csv.Error`, o estado do leitor não é confiável.

Nenhuma mensagem, `repr` ou exceção encadeada leva o caminho ou o conteúdo do arquivo: os erros de
leitura, de decodificação e do `csv` são descartados (`from None`), e só a linha é citada.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass

# Teto de linhas de alunos e de bytes do arquivo.
MAX_ROWS = 1000
MAX_BYTES = 4 * 1024 * 1024

# Arquivo da lista: as mensagens exatas (o CLI prefixa `Uso incorreto: `).
READ_REASON = "não foi possível ler o arquivo da lista."
SIZE_REASON = "o arquivo da lista passa de 4 MiB."
HEADER_REASON = ("a primeira linha do arquivo da lista deve ter exatamente as colunas ra, nome e email, "
                 "separadas por vírgula ou por ponto e vírgula.")
EMPTY_REASON = "o arquivo da lista não tem nenhuma linha de aluno."
TOO_MANY_REASON = "o arquivo da lista tem mais de 1.000 linhas de alunos."

_DELIMITERS = (",", ";")
# As colunas do cabeçalho (em ordem alfabética) e a chave de cada uma no pedido da API.
_HEADER = ["email", "nome", "ra"]
_KEYS = {"ra": "ra", "nome": "name", "email": "email"}
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
# O campo de um item da lista num erro da API (`rows[i]`, índice a partir de 0).
_ROW_FIELD = re.compile(r"rows\[(0|[1-9][0-9]*)\]")


def _utf8_reason(line: int) -> str:
    return f"o arquivo da lista não está em UTF-8 (linha {line})."


def _csv_reason(line: int) -> str:
    return f"o arquivo da lista não é um CSV válido na linha {line}."


def _columns_reason(line: int) -> str:
    return f"a linha {line} do arquivo da lista não tem as 3 colunas do cabeçalho."


class RosterFileError(Exception):
    """Arquivo da lista fora do formato; `reason` é fixa, sem caminho nem conteúdo."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Roster:
    """As linhas da lista, como estão no arquivo, e a linha física em que cada uma começa."""

    rows: list[dict[str, str]]  # {"ra", "name", "email"} como estão no arquivo
    lines: list[int]  # lines[i] = linha do arquivo em que rows[i] começa (cabeçalho = 1)


def _read_bytes(path: str) -> bytes:
    """Os bytes do arquivo, até `MAX_BYTES`; qualquer falha de abertura ou leitura (inexistente,
    diretório, sem permissão, caminho inválido) dá a mesma mensagem, sem o caminho."""
    try:
        with open(path, "rb") as file:
            raw = file.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        raise RosterFileError(READ_REASON) from None
    if len(raw) > MAX_BYTES:
        raise RosterFileError(SIZE_REASON)
    return raw


def _decode(raw: bytes) -> str:
    """O texto em `utf-8-sig` estrito (um BOM tolerado no início); fora de UTF-8, a linha física do
    primeiro byte inválido (`\\r\\n`, `\\r` sozinho e `\\n` contam uma quebra cada)."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # `exc.object` é o que o codec decodificou (sem o BOM), e `exc.start` é relativo a ele.
        before = exc.object[:exc.start]
        line = before.count(b"\n") + before.count(b"\r") - before.count(b"\r\n") + 1
        raise RosterFileError(_utf8_reason(line)) from None


def _header_columns(text: str) -> tuple[str, dict[str, int]] | None:
    """O separador e a posição de cada coluna, pela primeira linha física; `None` se nenhum separador
    dá exatamente `ra`, `nome` e `email`. Cada tentativa fica isolada do `csv.Error` da outra (ex.:
    `"ra";"nome";"email"` lido com `,`)."""
    first_line = _LINE_BREAK.split(text, maxsplit=1)[0]
    for delimiter in _DELIMITERS:
        try:
            cells = next(csv.reader([first_line], delimiter=delimiter, strict=True))
        except (csv.Error, StopIteration):
            continue
        lowered = [cell.lower() for cell in cells]
        if sorted(lowered) == _HEADER:
            return delimiter, {name: index for index, name in enumerate(lowered)}
    return None


def parse_roster(raw: bytes) -> Roster:
    """Analisa os bytes do arquivo da lista e devolve o `Roster`
    (ou levanta `RosterFileError`).

    Função pura, sem E/S: o `read_roster` do CLI e o servidor MCP (que lê o arquivo por conta própria) a chamam
    com os bytes já lidos. Acima de `MAX_BYTES`, `SIZE_REASON`. Devolve as linhas de alunos na ordem do arquivo,
    com os campos sem aparar, e a linha física em que cada uma começa. Fora do formato, levanta `RosterFileError`
    com a mensagem fixa do caso.
    """
    if len(raw) > MAX_BYTES:
        raise RosterFileError(SIZE_REASON)
    text = _decode(raw)
    header = _header_columns(text)
    if header is None:
        raise RosterFileError(HEADER_REASON)
    delimiter, position = header

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    rows: list[dict[str, str]] = []
    lines: list[int] = []
    previous = 0  # última linha física do registro anterior
    try:
        next(reader)  # o cabeçalho, já conferido
        previous = reader.line_num
        for record in reader:
            start, previous = previous + 1, reader.line_num
            if not any(record):  # `[]`, ou todos os campos vazios
                continue
            if len(record) != len(_HEADER):
                raise RosterFileError(_columns_reason(start))
            if len(rows) == MAX_ROWS:
                raise RosterFileError(TOO_MANY_REASON)
            rows.append({_KEYS[name]: record[index] for name, index in position.items()})
            lines.append(start)
    except csv.Error:
        # O registro com erro começa logo depois do último lido inteiro.
        raise RosterFileError(_csv_reason(previous + 1)) from None
    if not rows:
        raise RosterFileError(EMPTY_REASON)
    return Roster(rows=rows, lines=lines)


def read_roster(path: str) -> Roster:
    """Lê o arquivo da lista em `path` e o analisa com `parse_roster`.

    Abre e lê até `MAX_BYTES + 1` bytes (qualquer falha dá `READ_REASON`, sem o caminho); o comportamento
    observável é o da importação da lista.
    """
    return parse_roster(_read_bytes(path))


def row_line(field: object, lines: Sequence[int]) -> int | None:
    """A linha do arquivo de um campo `rows[i]` de um erro da API (`lines[i]`); `None` para qualquer
    outro campo (`rows`, `body`) e para um índice fora da lista."""
    match = _ROW_FIELD.fullmatch(field) if isinstance(field, str) else None
    if match is None:
        return None
    index = int(match[1])
    return lines[index] if index < len(lines) else None
