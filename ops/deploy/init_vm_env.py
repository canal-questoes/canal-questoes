"""Criação do `.env` da pilha e do perfil local do `cq` na VM.


Uso (na VM):

    python3 ops/deploy/init_vm_env.py --stack-dir /srv/cq/app --repo-hosts github.com
            [--engine-mem 1536m --engine-cpus 1.5]

Recusa se `<stack-dir>/.env` existir. Pede sem eco (`getpass`) a chave-mestra (43 caracteres base64url), o
usuário e a senha de app do Gmail; gera `CQ_DB_PASSWORD` e `CQ_ADMIN_TOKEN`; escreve o `.env` com
`os.open(…, O_CREAT|O_EXCL|O_WRONLY, 0o600)` e o `~/.config/cq/config.toml` (0600) com
`default_profile = "vm-local"` e `[profiles.vm-local]`. Nenhum valor gerado ou digitado é impresso.

Só biblioteca padrão. Importável sem efeito colateral.

**[I]** Escolhas da implementação, não fixadas de antemão:
- a ordem: argumentos → `.env` existente (antes de qualquer pergunta) → chave-mestra (validada logo depois de
  digitada, sem pedir o resto) → usuário e senha de app → os dois tokens → `.env` (criação exclusiva) → perfil;
- os limites do motor vão a `CQ_ENGINE_MEM_LIMIT` e `CQ_ENGINE_CPUS` (os nomes do `compose.yaml`), o usuário
  do Gmail a `CQ_SMTP_USERNAME` e a `CQ_SMTP_FROM`, a senha de app a `CQ_SMTP_PASSWORD`; a primeira chamada do
  `token_factory` vira `CQ_DB_PASSWORD` e a segunda `CQ_ADMIN_TOKEN` (o token do perfil);
- o `.env` sai sem aspas (`CHAVE=valor`); por isso um valor (digitado ou de argumento) vazio, com espaço
  no começo ou no fim, controle, aspas, `#`, `$`, `\\` ou crase é recusado com `invalid_arguments` (o Compose
  leria `#` como comentário e `$` como interpolação num valor sem aspas), e nada é escrito. O espaço no meio
  é aceito: a senha de app do Gmail costuma vir em grupos de quatro letras separados por espaço (o teste de
  privacidade usa essa forma), e o servidor aceita espaço em `CQ_SMTP_USERNAME`/`CQ_SMTP_PASSWORD`
  (`server/src/cq_server/config.py`, `_SMTP_CREDENTIAL`). **[I]** O Compose preserva o espaço interno de um
  valor sem aspas no `.env` (memória; não conferido na fonte);
- `--stack-dir` tem de ser um diretório existente; `--repo-hosts` é uma lista de nomes separados por vírgula;
  `--engine-mem` é um número com unidade opcional (`b`, `k`, `m`, `g`) e `--engine-cpus` um número positivo
  (o servidor e o Compose fazem a validação fina);
- o `config.toml` é escrito num temporário `0600` no mesmo diretório e trocado pelo `os.replace`: um
  `config.toml` anterior é substituído (numa reinstalação, o token antigo já não vale). Se o perfil não puder
  ser gravado, o `.env` que esta chamada criou é removido (a chamada pode ser repetida) e a saída é `internal`.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn, TextIO

# Chave → (código de saída, mensagem). A `ok` vai ao stdout.
INIT_ENV_MESSAGES: dict[str, tuple[int, str]] = {
    "ok": (0, "Arquivo .env e perfil local criados."),
    "exists": (64, "O arquivo .env já existe; nada foi alterado."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "invalid_master_key": (65, "A chave-mestra deve ter 43 caracteres base64url; nada foi escrito."),
    "internal": (1, "Erro interno ao criar o .env."),
}

# Os valores não secretos escritos no `.env` (os demais vêm dos argumentos e do `prompt`).
NON_SECRET_DEFAULTS: Mapping[str, str] = {
    "CQ_API_PORT": "8000",
    "CQ_SMTP_HOST": "smtp.gmail.com",
    "CQ_SMTP_PORT": "587",
    "CQ_SMTP_SECURITY": "starttls",
    "CQ_BACKUP_STATUS_DIR": "/var/backups/cq/state",
    "CQ_BACKUP_STATUS_FILE": "/run/cq-backup/last-backup.json",
}

# Os padrões dos limites do motor e o perfil local gravado.
DEFAULT_ENGINE_MEM = "1536m"
DEFAULT_ENGINE_CPUS = "1.5"
PROFILE_NAME = "vm-local"
PROFILE_API_URL = "http://127.0.0.1:8000"
PROFILE_COMPOSE_PROJECT = "cq"

ENV_FILE_NAME = ".env"
CONFIG_RELATIVE = Path(".config") / "cq" / "config.toml"

# Os textos das perguntas (fixos; nenhum leva valor), na ordem em que são feitas.
PROMPT_MASTER_KEY = "Chave-mestra (43 caracteres base64url): "
PROMPT_SMTP_USERNAME = "Usuário do Gmail: "
PROMPT_SMTP_PASSWORD = "Senha de app do Gmail: "

# A chave-mestra: 32 bytes em base64url, sem "=" (o mesmo padrão de `server/src/cq_server/config.py`).
MASTER_KEY_LENGTH = 43
MASTER_KEY_BYTES = 32
_MASTER_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
# Um valor que cabe sem aspas no `.env`: ASCII visível, sem `"`, `#`, `$`, `'`, `\` nem crase; espaço só
# entre dois desses caracteres (nunca no começo nem no fim).
_SAFE_CHARS = r"[\x21\x25\x26\x28-\x5b\x5d-\x5f\x61-\x7e]"
_SAFE_ENV_VALUE = re.compile(rf"{_SAFE_CHARS}+(?: {_SAFE_CHARS}+)*")
_ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
_HOSTS_PATTERN = re.compile(r"[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*")
_MEM_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)(?:[bkmgBKMG][bB]?)?")
_CPUS_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?")

_O_BINARY = getattr(os, "O_BINARY", 0)  # No Windows, sem ele o CRT traduziria `\n` em `\r\n`.
_FILE_MODE = 0o600
_DIR_MODE = 0o700


class _InvalidArguments(Exception):
    """Argumento de linha de comando inválido (vira `invalid_arguments`, sem eco do argumento)."""


class _Parser(argparse.ArgumentParser):
    """O `argparse` sem a mensagem nem o uso dele no stderr: o erro vira `_InvalidArguments`."""

    def error(self, message: str) -> NoReturn:
        raise _InvalidArguments()


def _emit(key: str, stdout: TextIO | None, stderr: TextIO | None) -> int:
    """Escreve a mensagem fixa de `key` (a `ok` no stdout, as demais no stderr) e devolve o código."""
    code, text = INIT_ENV_MESSAGES[key]
    if key == "ok":
        stream = stdout if stdout is not None else sys.stdout
    else:
        stream = stderr if stderr is not None else sys.stderr
    stream.write(text + "\n")
    stream.flush()
    return code


def _is_safe_value(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_ENV_VALUE.fullmatch(value))


def _valid_master_key(value: object) -> bool:
    if not isinstance(value, str) or not _MASTER_KEY_PATTERN.fullmatch(value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == MASTER_KEY_BYTES


def _valid_arguments(stack_dir: Path, repo_hosts: object, engine_mem: object, engine_cpus: object) -> bool:
    if not (isinstance(repo_hosts, str) and isinstance(engine_mem, str) and isinstance(engine_cpus, str)):
        return False
    mem = _MEM_PATTERN.fullmatch(engine_mem)
    if not (_HOSTS_PATTERN.fullmatch(repo_hosts) and mem and _CPUS_PATTERN.fullmatch(engine_cpus)):
        return False
    if float(engine_cpus) <= 0 or float(mem.group(1)) <= 0:
        return False
    return stack_dir.is_dir()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _write_exclusive(path: Path, data: bytes) -> None:
    """Cria `path` com `O_CREAT | O_EXCL` e modo `0600`; `FileExistsError` se ele já existir. Numa falha
    depois da criação, o arquivo criado aqui é removido."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, _FILE_MODE)
    try:
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, _FILE_MODE)  # Independe da umask.
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        _remove_quietly(path)
        raise


def _toml_string(value: str) -> str:
    """Uma string básica do TOML, com `\\`, `"` e os controles escapados."""
    out = ['"']
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def render_config(stack_dir: Path, token: str) -> str:
    """O texto do `config.toml` da VM: o perfil nomeado `vm-local` como padrão (no topo,
    `kind` e `stack_dir` seriam ignorados)."""
    return (
        f"default_profile = {_toml_string(PROFILE_NAME)}\n"
        "\n"
        f"[profiles.{PROFILE_NAME}]\n"
        'kind = "local"\n'
        f"api_url = {_toml_string(PROFILE_API_URL)}\n"
        f"stack_dir = {_toml_string(os.path.abspath(stack_dir))}\n"
        f"compose_project = {_toml_string(PROFILE_COMPOSE_PROJECT)}\n"
        f"token = {_toml_string(token)}\n"
    )


def _write_config(home: Path, text: str) -> None:
    """Grava o `config.toml` (`0600`) por um temporário exclusivo no mesmo diretório e `os.replace`."""
    config = home / CONFIG_RELATIVE
    config.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    temporary = config.with_name(f".{config.name}.{secrets.token_hex(8)}.tmp")
    _write_exclusive(temporary, text.encode("utf-8"))
    try:
        os.replace(temporary, config)
    except BaseException:
        _remove_quietly(temporary)
        raise


def init(stack_dir: Path, *, repo_hosts: str, engine_mem: str = DEFAULT_ENGINE_MEM,
         engine_cpus: str = DEFAULT_ENGINE_CPUS, prompt: Callable[[str], str] = getpass.getpass,
         token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32), home: Path,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Cria o `.env` e o perfil local; devolve o código de `INIT_ENV_MESSAGES`.
    `stdout`/`stderr` `None` → os do processo, resolvidos na chamada."""
    try:
        stack_dir = Path(stack_dir)
        if not _valid_arguments(stack_dir, repo_hosts, engine_mem, engine_cpus):
            return _emit("invalid_arguments", stdout, stderr)
        env_path = stack_dir / ENV_FILE_NAME
        # Antes de qualquer pergunta: nenhum segredo é pedido à toa (a criação exclusiva confere de novo).
        if os.path.lexists(env_path):
            return _emit("exists", stdout, stderr)

        master_key = prompt(PROMPT_MASTER_KEY)
        if not _valid_master_key(master_key):
            return _emit("invalid_master_key", stdout, stderr)
        smtp_username = prompt(PROMPT_SMTP_USERNAME)
        smtp_password = prompt(PROMPT_SMTP_PASSWORD)
        if not (_is_safe_value(smtp_username) and _is_safe_value(smtp_password)):
            return _emit("invalid_arguments", stdout, stderr)

        db_password = token_factory()
        admin_token = token_factory()
        if not (_is_safe_value(db_password) and _is_safe_value(admin_token)) or db_password == admin_token:
            return _emit("internal", stdout, stderr)

        values = {
            **NON_SECRET_DEFAULTS,
            "CQ_MASTER_KEY": master_key,
            "CQ_DB_PASSWORD": db_password,
            "CQ_ADMIN_TOKEN": admin_token,
            "CQ_SMTP_USERNAME": smtp_username,
            "CQ_SMTP_PASSWORD": smtp_password,
            "CQ_SMTP_FROM": smtp_username,
            "CQ_REPO_ALLOWED_HOSTS": repo_hosts,
            "CQ_ENGINE_MEM_LIMIT": engine_mem,
            "CQ_ENGINE_CPUS": engine_cpus,
        }
        env_text = render_env(values)
        config_text = render_config(stack_dir, admin_token)

        try:
            _write_exclusive(env_path, env_text.encode("utf-8"))
        except FileExistsError:
            return _emit("exists", stdout, stderr)
        try:
            _write_config(home, config_text)
        except BaseException:
            _remove_quietly(env_path)  # O `.env` é desta chamada (criação exclusiva).
            raise
        return _emit("ok", stdout, stderr)
    except Exception:  # noqa: BLE001 - só a mensagem fixa; nenhuma exceção (nem caminho) chega à saída.
        return _emit("internal", stdout, stderr)


def render_env(values: Mapping[str, str]) -> str:
    """O texto do `.env` (`CHAVE=valor`, uma por linha) só com os valores de `values`.

    Levanta `ValueError` (sem o valor) para uma chave fora de `[A-Z_][A-Z0-9_]*` ou um valor que não cabe sem
    aspas (ver o docstring do módulo)."""
    lines = []
    for key, value in values.items():
        if not _ENV_KEY.fullmatch(key) or not _is_safe_value(value):
            raise ValueError("valor fora da forma do .env")
        lines.append(f"{key}={value}\n")
    return "".join(lines)


def _parser() -> _Parser:
    parser = _Parser(prog="init_vm_env.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--stack-dir", required=True)
    parser.add_argument("--repo-hosts", required=True)
    parser.add_argument("--engine-mem", default=DEFAULT_ENGINE_MEM)
    parser.add_argument("--engine-cpus", default=DEFAULT_ENGINE_CPUS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Linha de comando; devolve o código de saída."""
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except _InvalidArguments:
        return _emit("invalid_arguments", None, None)
    try:
        home = Path.home()
    except Exception:  # noqa: BLE001 - sem `HOME` resolvível.
        return _emit("internal", None, None)
    return init(Path(args.stack_dir), repo_hosts=args.repo_hosts, engine_mem=args.engine_mem,
                engine_cpus=args.engine_cpus, home=home)


if __name__ == "__main__":
    sys.exit(main())
