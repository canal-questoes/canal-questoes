"""Três comportamentos do servidor MCP e os casos
que os cobrem.


1. **O silêncio do stderr** (o stderr só com as linhas fixas):
   `quiet_libraries()` põe um `NullHandler` na raiz durante o contexto e o tira na saída. Sem ele, um registro de
   WARNING ou acima de um logger fora de `MCP_LOGGERS` (por exemplo `asyncio`) propaga até a raiz sem tratador e cai
   no `logging.lastResort`, que o escreve no stderr, fora das linhas fixas de `MCP_STDERR_MESSAGES`.
   O cenário roda num **subprocesso** Python limpo (`sys.executable -I -c …`): **[F]** dentro de um teste do pytest,
   a raiz já tem tratadores (os do plugin de logging), e um aviso do `asyncio` não chega ao stderr real nem sem o
   `NullHandler` (um teste em processo passaria de qualquer jeito). **[F]** Com `-I`, o filho não leva o `PYTHONPATH`
   nem o diretório atual ao `sys.path`, mas acha o `cq_client` pelo `site-packages` do ambiente, e a raiz começa sem
   tratadores, com o `lastResort` ativo (os três conferidos no Windows 11 com Python 3.12.3, 2026-10-29). O controle
   positivo é o mesmo aviso fora do contexto, que sai no stderr.
2. **O caminho de rede e o de dispositivo no Windows** (na importação da
   lista por caminho de arquivo): `read_import_file` recusa como `ImportFileError("not_regular")`, **antes** de qualquer `os.stat`,
   o caminho UNC (`\\\\host\\share\\x.csv`, `//host/share/x.csv`) e o de dispositivo (`\\\\?\\C:\\x.csv`,
   `\\\\.\\C:\\x.csv`) quando `os.name == "nt"`; pela ferramenta `import_students`, o resultado é o erro de execução
   fixo de `file.not_regular`, sem nenhum pedido à API. **[I]** O `stat` de um caminho UNC abriria uma conexão SMB com
   um host qualquer e poderia entregar a credencial NTLM do operador. O `os.stat` e o `os.open` do módulo `os` viram
   dublês que, num caminho de duas barras no início, registram o acesso e levantam `AssertionError`: nada é tocado.

**Os dois sistemas, sem `skip`**: a suíte roda no Windows. No POSIX, a contrabarra não separa
diretórios, e **[F]** (`pathlib.PurePosixPath`, Python 3.12) os caminhos que começam por contrabarra são relativos
(→ `not_absolute`), mas os que começam por `/` (`//host/…` e `/\\host\\…`) são absolutos e locais, sem SMB: o `stat`
é legítimo, e o dublê do POSIX responde `FileNotFoundError` (→ `not_found`). A expectativa de cada caminho é escrita
conforme `os.name`.

**[F]** Sem cada proteção (a `quiet_libraries` sem o `NullHandler` na raiz; a
`read_import_file` sem a checagem do UNC), os dois testes falham; com elas, passam (conferido no
Windows 11 com Python 3.12.3, 2026-10-29). **[I]** O ramo do POSIX não foi executado.

3. **A falha ao carregar o servidor MCP**: se o import de
   `cq_client.mcp_server` falhar (SDK ausente ou quebrado), a folha `cq mcp` escreve só a linha fixa de falha interna
   (`MCP_STDERR_MESSAGES["internal"]`) e sai com 1, sem nada no stdout.
   O teste confere também:
   as três linhas fixas do stderr, pelo literal do teste (`CONTRACT_STDERR_LINES`); o
   `_RootGroup` não escreve linha própria de falha inesperada, fora do conjunto fechado do stderr.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest
from mcp.client.stdio import get_default_environment

from cq_client.mcp_server import MCP_LOGGERS, MCP_MESSAGES, read_import_file
from tests.helpers.api import ADMIN_TOKEN, create_offering
from tests.helpers.mcp_client import HOME_VARIABLES, MODES, scan
from tests.unit import test_mcp_import_file as import_file_tests

# --------------------------------------------------------------------------------------------------
# 1. O silêncio do stderr num processo limpo

# Os textos dos registros: o de dentro do contexto nunca sai; o de fora (controle) sai pelo `lastResort`.
INSIDE_SENTINEL = "registro-sentinela-dentro-Kv52"
CONTROL_SENTINEL = "registro-controle-fora-Kv52"
# Um logger de nome qualquer, além do `asyncio` (o exemplo realista): o silêncio vale para a raiz, não para uma lista
# de nomes (o stderr só tem as linhas fixas, venha o registro de onde vier).
OTHER_LOGGER = "biblioteca.qualquer"

# Teto do subprocesso (a partida do Python com o `import` do `mcp` e o cenário).
CHILD_TIMEOUT_SECONDS = 60

# O cenário no filho: `argv[1]` = o texto de dentro, `argv[2]` = o do controle. Os registros de dentro (WARNING e
# ERROR com rastreamento no `asyncio`, CRITICAL num logger qualquer) e o de fora vão para os tratadores reais; o
# stdout recebe só uma linha JSON com o estado da raiz antes e depois do contexto.
QUIET_CHILD_CODE = """
import json, logging, sys
from cq_client.mcp_server import quiet_libraries

inside, control, other_name = sys.argv[1], sys.argv[2], sys.argv[3]
root = logging.getLogger()

def snapshot():
    return {"handlers": [id(h) for h in root.handlers], "level": root.level, "disable": root.manager.disable,
            "last_resort": id(logging.lastResort)}

clean = {"root_handlers": len(root.handlers), "last_resort_set": logging.lastResort is not None}
before = snapshot()
asyncio_logger = logging.getLogger("asyncio")
with quiet_libraries():
    asyncio_logger.warning("%s (warning)", inside)
    try:
        raise RuntimeError(inside + " (traceback)")
    except RuntimeError:
        asyncio_logger.error("%s (error)", inside, exc_info=True)
    logging.getLogger(other_name).critical("%s (critical)", inside)
    sys.stderr.flush()
after = snapshot()
asyncio_logger.warning("%s (warning)", control)
sys.stderr.flush()
print(json.dumps({"clean": clean, "before": before, "after": after}))
"""


def test_quiet_libraries_keeps_stderr_clean_for_other_loggers_in_a_clean_process(tmp_path: pathlib.Path) -> None:
    """Stderr só com as linhas fixas: num processo Python limpo (`-I`, raiz sem
    tratadores, `lastResort` ativo; pré-condição conferida), dentro de `quiet_libraries()`, nem um WARNING nem um ERROR
    com rastreamento do `asyncio` (fora de `MCP_LOGGERS`), nem um CRITICAL de um logger qualquer, escrevem algo no
    stderr; fora do contexto, o mesmo aviso sai no stderr (controle positivo: o `lastResort` volta); os tratadores, o
    nível e o desligamento da raiz e o `lastResort` voltam ao que eram; o stdout tem só a linha do relatório."""
    # Pré-condição: o logger qualquer não é nem descende de um de `MCP_LOGGERS`.
    assert not any(OTHER_LOGGER == name or OTHER_LOGGER.startswith(name + ".") for name in MCP_LOGGERS)
    env =get_default_environment() | {name: str(tmp_path) for name in HOME_VARIABLES}
    done = subprocess.run([sys.executable, "-I", "-c", QUIET_CHILD_CODE, INSIDE_SENTINEL, CONTROL_SENTINEL,
                           OTHER_LOGGER],
                          cwd=str(tmp_path), env=env, capture_output=True, timeout=CHILD_TIMEOUT_SECONDS, check=False)
    err = done.stderr.decode("utf-8", errors="replace")
    out = done.stdout.decode("utf-8", errors="replace")
    assert done.returncode == 0, err[-2000:]

    assert INSIDE_SENTINEL not in err, f"um registro de dentro do contexto saiu no stderr:\n{err[-2000:]}"
    assert err.splitlines() == [f"{CONTROL_SENTINEL} (warning)"], err[-2000:]
    assert scan(err + out, import_file_tests._secrets()) == []

    lines = out.splitlines()
    assert len(lines) == 1, out[-2000:]
    report = json.loads(lines[0])
    assert report["clean"] == {"root_handlers": 0, "last_resort_set": True}
    assert report["after"] == report["before"]


# --------------------------------------------------------------------------------------------------
# 2. O caminho de rede e o de dispositivo no Windows

# O host nunca é procurado (os dublês cortam o acesso); **[I]** o domínio `.invalid` é reservado e não resolve
# (RFC 2606), por precaução. O diretório e o arquivo têm os nomes de alunos canário do teste vizinho: o caminho nunca
# sai em mensagem.
_TAIL = f"{import_file_tests.CANARY_DIR}\\{import_file_tests.CANARY_FILE}"
_TAIL_SLASH = f"{import_file_tests.CANARY_DIR}/{import_file_tests.CANARY_FILE}"

# (id, caminho, chave esperada no POSIX). No Windows, todos dão `not_regular`.
NETWORK_PATHS: list[tuple[str, str, str]] = [
    # \\host.invalid\<dir>\<arquivo>
    ("unc-contrabarra", "\\\\host.invalid\\" + _TAIL, "not_absolute"),
    # //host.invalid/<dir>/<arquivo>
    ("unc-barra", "//host.invalid/" + _TAIL_SLASH, "not_found"),
    # /\host.invalid\<dir>\<arquivo> (as barras misturadas: no Windows, o mesmo UNC)
    ("unc-barras-misturadas", "/\\host.invalid\\" + _TAIL, "not_found"),
    # \/host.invalid/<dir>/<arquivo> (a ordem inversa das barras misturadas):
    # no Windows, o mesmo UNC; no POSIX, relativo (começa por contrabarra)
    ("unc-barras-invertidas", "\\/host.invalid/" + _TAIL_SLASH, "not_absolute"),
    # \\?\C:\<dir>\<arquivo>
    ("dispositivo-interrogacao", "\\\\?\\C:\\" + _TAIL, "not_absolute"),
    # \\.\C:\<dir>\<arquivo>
    ("dispositivo-ponto", "\\\\.\\C:\\" + _TAIL, "not_absolute"),
    # \\?\UNC\host.invalid\<dir>\<arquivo> (o UNC na forma de dispositivo)
    ("dispositivo-unc", "\\\\?\\UNC\\host.invalid\\" + _TAIL, "not_absolute"),
]

# As três linhas fixas do stderr do `cq mcp`, literais.
CONTRACT_STDERR_LINES = {
    "started": "Servidor MCP do canal-questoes iniciado.",
    "stopped": "Servidor MCP do canal-questoes encerrado.",
    "internal": "Falha interna numa ferramenta; detalhes omitidos.",
}

KEY_TEXTS = {"not_regular": import_file_tests.NOT_REGULAR_TEXT, "not_absolute": import_file_tests.NOT_ABSOLUTE_TEXT,
             "not_found": import_file_tests.NOT_FOUND_TEXT}


def _is_network_form(path: Any) -> bool:
    """O caminho começa por duas barras, em qualquer combinação de `/` e `\\` (rede ou dispositivo no Windows)."""
    return os.fsdecode(path).replace("/", "\\").startswith("\\\\")


def _forbid_network_access(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Troca `os.stat` e `os.open` do módulo `os`, até o fim do teste, por dublês que, num caminho de duas barras no
    início, registram `(função, caminho)` e não tocam o sistema: no Windows, os dois levantam `AssertionError`; no
    POSIX, onde `//…` e `/\\…` são caminhos locais (sem SMB), o `os.stat` levanta `FileNotFoundError`, e o `os.open`
    continua proibido. Os demais caminhos e os descritores passam aos originais. Devolve o registro."""
    real_stat, real_open = os.stat, os.open
    touched: list[tuple[str, str]] = []

    def guarded(path: Any) -> bool:
        return isinstance(path, (str, bytes, os.PathLike)) and _is_network_form(path)

    def fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if guarded(path):
            touched.append(("stat", os.fsdecode(path)))
            if os.name == "nt":
                raise AssertionError("os.stat chamado num caminho de rede ou de dispositivo")
            raise FileNotFoundError(errno.ENOENT, "caminho local inexistente (dublê)")
        return real_stat(path, *args, **kwargs)

    def fake_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if guarded(path):
            touched.append(("open", os.fsdecode(path)))
            raise AssertionError("os.open chamado num caminho de rede ou de dispositivo")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "open", fake_open)
    return touched


def _expected(path: str, posix_key: str) -> tuple[str, list[tuple[str, str]]]:
    """A chave esperada e os acessos esperados: no Windows, `not_regular` sem acesso nenhum; no POSIX, a chave do
    caso, com o `stat` só no caminho absoluto (o local `//…` ou `/\\…`)."""
    if os.name == "nt":
        return "not_regular", []
    return posix_key, ([] if posix_key == "not_absolute" else [("stat", path)])


@pytest.mark.parametrize("mode", MODES)
def test_network_and_device_paths_are_refused_before_any_stat(mode: str, client: Any, tmp_path: pathlib.Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """no Windows, cada caminho de rede (UNC: contrabarras, barras,
    barras misturadas) ou de dispositivo (`\\\\?\\`, `\\\\.\\`, `\\\\?\\UNC\\`) é absoluto (pré-condição: só a checagem
    do UNC o recusa) e é recusado como `ImportFileError("not_regular")`, com a mensagem fixa e sem o caminho, sem
    nenhum `os.stat` nem `os.open` nele; pela `import_students`, nas duas eras, o resultado é o erro de execução
    `"[file.not_regular] O caminho não é um arquivo comum."`, sem `structuredContent`, sem nenhum pedido à API e sem
    o caminho, de novo sem acesso nenhum. No POSIX, a expectativa de cada caminho segue o módulo (`not_absolute` ou,
    nos absolutos locais, `not_found` pelo dublê). Controle positivo: um arquivo local de caminho absoluto no
    `tmp_path` (com contrabarras e com barras) continua sendo lido, sem passar pelo registro dos dublês."""
    offering = create_offering(client, ADMIN_TOKEN)
    touched = _forbid_network_access(monkeypatch)

    for case_id, path, posix_key in NETWORK_PATHS:
        assert pathlib.PureWindowsPath(path).is_absolute(), case_id
        assert pathlib.PurePosixPath(path).is_absolute() == (posix_key != "not_absolute"), case_id
        key, accesses = _expected(path, posix_key)

        touched.clear()
        import_file_tests._assert_import_error(path, key)
        assert touched == accesses, case_id

        touched.clear()
        outcome, seen = import_file_tests._import(tmp_path, client, mode, offering["code"], path)
        assert outcome.protocol_code is None, (case_id, outcome.raw)
        assert outcome.is_error is True, case_id
        assert outcome.structured is None, case_id
        assert outcome.text == KEY_TEXTS[key], case_id
        assert seen == [], case_id
        assert touched == accesses, case_id
        import_file_tests._assert_clean(outcome, path)

    assert KEY_TEXTS["not_regular"] == f"[file.not_regular] {MCP_MESSAGES['file.not_regular']}"

    data = import_file_tests.HEADER + import_file_tests.NEUTRAL_ROW
    local = import_file_tests._write(tmp_path, data)
    touched.clear()
    for form in (str(local), local.as_posix()):
        assert pathlib.Path(form).is_absolute(), form
        assert read_import_file(form) == data, form
    assert touched == []


def test_mcp_leaf_writes_only_the_fixed_line_when_the_server_does_not_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 3 do docstring do módulo: com o import de `cq_client.mcp_server` falhando, `cq mcp` sai com 1, o
    stdout vazio e o stderr exatamente a linha fixa de falha interna, a mesma de `MCP_STDERR_MESSAGES["internal"]`.
    O import falha por `sys.modules["cq_client.mcp_server"] = None` (o `import` levanta `ImportError`) e pela retirada
    do atributo do pacote, que o `from cq_client import mcp_server` leria antes de importar."""
    import cq_client
    from cq_client import cli as cq_cli
    from cq_client.mcp_server import MCP_STDERR_MESSAGES
    from tests.helpers.cli_runner import invoke_cli

    monkeypatch.delattr(cq_client, "mcp_server", raising=False)
    monkeypatch.setitem(sys.modules, "cq_client.mcp_server", None)
    result = invoke_cli([cq_cli.MCP_COMMAND])
    assert result.exit_code == 1, (result.exit_code, result.stderr[-500:])
    assert result.stdout == ""
    # As linhas fixas do stderr pelo literal do teste, não pela
    # constante do código; uma constante mudada (por exemplo, com o detalhe da exceção) reprova.
    assert dict(MCP_STDERR_MESSAGES) == CONTRACT_STDERR_LINES
    assert result.stderr == CONTRACT_STDERR_LINES["internal"] + "\n", result.stderr[-500:]
