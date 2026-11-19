"""Testes de `ops/deploy/init_vm_env.py`, que cria o `.env` da pilha e o perfil local da VM a partir
dos valores digitados e gerados.

Com `prompt` e `token_factory` injetados e `home` no `tmp_path` (nenhum `getpass` real, nenhum `~` do
desenvolvedor). O que se confere:
- recusa se `<stack-dir>/.env` existir (64, o arquivo intacto, nada pedido nem escrito);
- os valores digitados e gerados no `.env`, com os valores não secretos fixos, e nada além deles;
- criação exclusiva (`O_CREAT | O_EXCL`): um `.env` criado por outro processo entre a conferência e a escrita
  (simulado pelo `prompt`) faz a chamada recusar, sem tocar no arquivo do outro; modo `0600` no POSIX;
- o `~/.config/cq/config.toml` (`0600` no POSIX) com `default_profile = "vm-local"` e `[profiles.vm-local]`,
  conferido pelo perfil efetivo do `resolve_profile` (`kind` local, `stack_dir`, `compose_project` `cq`, o token
  gerado);
- chave-mestra fora de 43 caracteres base64url → `invalid_master_key`, nada escrito;
- cada saída com o código e o texto exatos esperados, e stdout e stderr só com eles, varridos
  por `find_secrets` com todos os valores digitados e gerados;
- `render_env` sem nenhum valor fora do mapeamento.

**[I]** Escolhas deste teste:
- a ordem das perguntas é chave-mestra, usuário, senha de app; o `prompt` falso responde por
  ordem de chamada;
- os limites do motor vão a `CQ_ENGINE_MEM_LIMIT` e `CQ_ENGINE_CPUS` (os nomes do `compose.yaml`), o usuário do
  Gmail a `CQ_SMTP_USERNAME` e a `CQ_SMTP_FROM`, a senha de app a `CQ_SMTP_PASSWORD`;
- o token do perfil é o `CQ_ADMIN_TOKEN` gerado; qual das duas chamadas do `token_factory` vira a senha do banco e
  qual vira o token não é fixado;
- cada mensagem é uma linha (o texto e `\\n`), a `ok` no stdout e as recusas no stderr; o `.env` tem a forma
  `CHAVE=valor`, uma por linha, sem aspas (o docstring do esqueleto);
- a recusa do `.env` existente vem antes das perguntas (nenhum segredo é pedido à toa); numa recusa, nem o `.env`
  nem o `config.toml` ficam escritos.
"""

from __future__ import annotations

import io
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.helpers.canary import find_secrets
from tests.helpers.ops import clean_env, load_ops, run_main

init_vm_env = load_ops("ops/deploy/init_vm_env.py")

MESSAGES = init_vm_env.INIT_ENV_MESSAGES
# Os códigos e os textos são contrato.
EXPECTED_MESSAGES = {
    "ok": (0, "Arquivo .env e perfil local criados."),
    "exists": (64, "O arquivo .env já existe; nada foi alterado."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "invalid_master_key": (65, "A chave-mestra deve ter 43 caracteres base64url; nada foi escrito."),
    "internal": (1, "Erro interno ao criar o .env."),
}
# os valores não secretos fixos.
EXPECTED_DEFAULTS = {
    "CQ_API_PORT": "8000",
    "CQ_SMTP_HOST": "smtp.gmail.com",
    "CQ_SMTP_PORT": "587",
    "CQ_SMTP_SECURITY": "starttls",
    "CQ_BACKUP_STATUS_DIR": "/var/backups/cq/state",
    "CQ_BACKUP_STATUS_FILE": "/run/cq-backup/last-backup.json",
}

REPO_HOSTS = "github.com,repos.cq.test"
ENGINE_MEM = "1g"
ENGINE_CPUS = "1.25"
# Valores digitados de teste (fictícios): o usuário do Gmail num domínio reservado e uma senha de app sem espaços.
GMAIL_USER = "conta.do.piloto@piloto.example.org"
APP_PASSWORD = "qwertasdfgzxcvbn"


def _master_key() -> str:
    import secrets

    key = secrets.token_urlsafe(32)
    assert len(key) == 43
    return key


class FakePrompt:
    """O `getpass` falso: responde por ordem de chamada e registra os textos perguntados."""

    def __init__(self, answers: list[str], *, on_call: Callable[[int], None] | None = None) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.on_call = on_call

    def __call__(self, text: str) -> str:
        self.asked.append(text)
        if self.on_call is not None:
            self.on_call(len(self.asked))
        assert self.answers, "o init_vm_env pediu mais valores do que o esperado"
        return self.answers.pop(0)


class FakeTokens:
    """O `token_factory` falso: tokens distintos (43 caracteres, como o `token_urlsafe(32)`)."""

    def __init__(self) -> None:
        self.made: list[str] = []

    def __call__(self) -> str:
        import secrets

        token = "tk" + secrets.token_urlsafe(31)[:41]
        assert len(token) == 43
        self.made.append(token)
        return token


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assert "=" in line, "linha do .env fora da forma CHAVE=valor"
        key, value = line.split("=", 1)
        assert key not in values, f"chave repetida no .env: {key}"
        values[key] = value
    return values


def _lines(stream: io.StringIO) -> list[str]:
    return stream.getvalue().splitlines()


def _config_path(home: Path) -> Path:
    return home / ".config" / "cq" / "config.toml"


def _assert_private(path: Path) -> None:
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, f"{path.name} sem modo 0600"


def _run(stack_dir: Path, home: Path, prompt: FakePrompt, tokens: FakeTokens,
         **kwargs: str) -> tuple[int, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    code = init_vm_env.init(stack_dir, repo_hosts=kwargs.get("repo_hosts", REPO_HOSTS),
                            engine_mem=kwargs.get("engine_mem", ENGINE_MEM),
                            engine_cpus=kwargs.get("engine_cpus", ENGINE_CPUS), prompt=prompt, token_factory=tokens,
                            home=home, stdout=out, stderr=err)
    return code, out, err


@pytest.fixture
def dirs(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    stack_dir = tmp_path / "srv" / "cq" / "app"
    stack_dir.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    yield stack_dir, home


def test_writes_env_and_local_profile(dirs: tuple[Path, Path]) -> None:
    """o `.env` com os valores digitados, os gerados e os fixos, e nada mais; o perfil `vm-local`
    efetivo; os dois arquivos `0600` no POSIX; stdout só com a `ok`, stderr vazio, nenhum valor nas saídas."""
    from cq_client.profile import resolve_profile

    stack_dir, home = dirs
    # As tabelas reais do esqueleto (na mesma função do caso vermelho).
    assert MESSAGES == EXPECTED_MESSAGES
    assert dict(init_vm_env.NON_SECRET_DEFAULTS) == EXPECTED_DEFAULTS

    master_key = _master_key()
    prompt = FakePrompt([master_key, GMAIL_USER, APP_PASSWORD])
    tokens = FakeTokens()
    code, out, err = _run(stack_dir, home, prompt, tokens)

    assert (code, _lines(out), _lines(err)) == (0, [EXPECTED_MESSAGES["ok"][1]], [])
    assert len(prompt.asked) == 3
    assert len(tokens.made) == 2 and len(set(tokens.made)) == 2

    env_path = stack_dir / ".env"
    env = _parse_env(env_path.read_text(encoding="utf-8"))
    generated = {env.get("CQ_DB_PASSWORD"), env.get("CQ_ADMIN_TOKEN")}
    assert generated == set(tokens.made)
    assert env == {
        **EXPECTED_DEFAULTS,
        "CQ_MASTER_KEY": master_key,
        "CQ_DB_PASSWORD": env["CQ_DB_PASSWORD"],
        "CQ_ADMIN_TOKEN": env["CQ_ADMIN_TOKEN"],
        "CQ_SMTP_USERNAME": GMAIL_USER,
        "CQ_SMTP_PASSWORD": APP_PASSWORD,
        "CQ_SMTP_FROM": GMAIL_USER,
        "CQ_REPO_ALLOWED_HOSTS": REPO_HOSTS,
        "CQ_ENGINE_MEM_LIMIT": ENGINE_MEM,
        "CQ_ENGINE_CPUS": ENGINE_CPUS,
    }
    _assert_private(env_path)

    config = _config_path(home)
    assert config.is_file()
    _assert_private(config)
    profile = resolve_profile({"CQ_CONFIG": str(config)})
    assert profile.name == "vm-local" and profile.named
    assert profile.kind == "local"
    assert profile.api_url == "http://127.0.0.1:8000"
    assert profile.stack_dir is not None and Path(profile.stack_dir).resolve() == stack_dir.resolve()
    assert profile.compose_project == "cq"
    assert profile.token == env["CQ_ADMIN_TOKEN"]

    values = [master_key, GMAIL_USER, APP_PASSWORD, *tokens.made]
    assert find_secrets(out.getvalue() + err.getvalue() + "\n".join(prompt.asked), values) == []


def test_refuses_when_env_already_exists(dirs: tuple[Path, Path]) -> None:
    """`<stack-dir>/.env` existente → `exists` (64), o arquivo intacto, nada pedido, nenhum perfil."""
    stack_dir, home = dirs
    env_path = stack_dir / ".env"
    original = b"# .env de outra instalacao\nCQ_API_PORT=8000\n"
    env_path.write_bytes(original)
    prompt = FakePrompt([_master_key(), GMAIL_USER, APP_PASSWORD])
    tokens = FakeTokens()

    code, out, err = _run(stack_dir, home, prompt, tokens)

    assert (code, _lines(out), _lines(err)) == (64, [], [EXPECTED_MESSAGES["exists"][1]])
    assert env_path.read_bytes() == original
    assert prompt.asked == [] and tokens.made == []
    assert not _config_path(home).exists()


def test_exclusive_creation_refuses_env_created_concurrently(dirs: tuple[Path, Path]) -> None:
    """Criação exclusiva (`O_CREAT | O_EXCL`): outro processo cria o `.env` depois da conferência e antes da escrita
    (durante as perguntas) → a chamada recusa com `exists`, sem sobrescrever o arquivo do outro, e sem perfil."""
    stack_dir, home = dirs
    env_path = stack_dir / ".env"
    concurrent = b"CQ_API_PORT=9000\n"

    def create_concurrently(call: int) -> None:
        if call == 1:
            env_path.write_bytes(concurrent)

    master_key = _master_key()
    prompt = FakePrompt([master_key, GMAIL_USER, APP_PASSWORD], on_call=create_concurrently)
    tokens = FakeTokens()

    code, out, err = _run(stack_dir, home, prompt, tokens)

    assert (code, _lines(out), _lines(err)) == (64, [], [EXPECTED_MESSAGES["exists"][1]])
    assert env_path.read_bytes() == concurrent
    assert not _config_path(home).exists()
    values = [master_key, GMAIL_USER, APP_PASSWORD, *tokens.made]
    assert find_secrets(out.getvalue() + err.getvalue(), values) == []


@pytest.mark.parametrize("bad_key", [
    pytest.param("a" * 42, id="42-caracteres"),
    pytest.param("a" * 44, id="44-caracteres"),
    pytest.param("a" * 42 + "+", id="caractere-fora-do-base64url"),
    pytest.param("a" * 42 + "=", id="preenchimento"),
    pytest.param("", id="vazia"),
])
def test_invalid_master_key_writes_nothing(dirs: tuple[Path, Path], bad_key: str) -> None:
    """chave-mestra fora de 43 caracteres base64url → `invalid_master_key` (65), nenhum arquivo."""
    stack_dir, home = dirs
    prompt = FakePrompt([bad_key, GMAIL_USER, APP_PASSWORD])
    tokens = FakeTokens()

    code, out, err = _run(stack_dir, home, prompt, tokens)

    assert (code, _lines(out), _lines(err)) == (65, [], [EXPECTED_MESSAGES["invalid_master_key"][1]])
    assert not (stack_dir / ".env").exists()
    assert not _config_path(home).exists()
    values = [value for value in (bad_key, GMAIL_USER, APP_PASSWORD, *tokens.made) if value]
    assert find_secrets(out.getvalue() + err.getvalue(), values) == []


def test_render_env_only_given_values() -> None:
    """`render_env`: `CHAVE=valor`, uma por linha, só com os valores do mapeamento (nenhum padrão acrescentado)."""
    values = {"CQ_API_PORT": "8123", "CQ_REPO_ALLOWED_HOSTS": "repos.cq.test"}
    text = init_vm_env.render_env(values)
    assert _parse_env(text) == values
    assert text.endswith("\n")


def test_main_rejects_unknown_option() -> None:
    """argumento inválido → `invalid_arguments` (64) no stderr, nada perguntado."""
    result = run_main(init_vm_env, ["--opcao-desconhecida"], env=clean_env())
    assert result.code == EXPECTED_MESSAGES["invalid_arguments"][0]
    assert EXPECTED_MESSAGES["invalid_arguments"][1] in result.stderr.splitlines()
    assert result.stdout == ""
