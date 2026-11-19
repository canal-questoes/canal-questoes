"""Perfil do CLI: endereço da API e token.

Precedência, campo a campo: `CQ_API_URL`/`CQ_TOKEN` do `env` recebido > arquivo de perfil TOML
(chaves `api_url` e `token`) > padrão (`http://127.0.0.1:8000`, sem token). Variável vazia vale como
ausente. O arquivo é `CQ_CONFIG` (se presente e não vazio) ou `click.get_app_dir("cq")/config.toml`,
calculado sobre o ambiente do **processo**.

Os erros (`ProfileError`) têm mensagens fixas: nunca trazem o conteúdo do arquivo nem o valor do
endereço, que podem carregar o token ou credenciais.

Perfis nomeados (`profiles.<nome>`, `default_profile`, `CQ_PROFILE`, a opção `--profile`) são
resolvidos por `resolve_profile` num `EffectiveProfile`.
`Profile`, `load_profile` e `default_config_path` seguem o comportamento do perfil implícito.
Este módulo não importa `click` no carregamento: o import
fica dentro de `default_config_path`.
"""

from __future__ import annotations

import pathlib
import re
import tomllib
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx2

from cq_client.api import clean_base_url

DEFAULT_API_URL = "http://127.0.0.1:8000"
CONFIG_FILE_NAME = "config.toml"
APP_NAME = "cq"
# ASCII imprimível, com caractere visível nas pontas: o que pode ir num cabeçalho
# `Authorization: Bearer` (um token errado, mas enviável, é recusado pela própria API com 401; só o
# que nem pode ser enviado para aqui). O transporte (h11) recusa valor de cabeçalho que termina em
# espaço, o que viraria "API inalcançável" com a API no ar; o espaço no começo, que ele enviaria,
# também é recusado, porque nenhum segredo gerado pela API (`cq_` + base64url) começa ou termina
# com espaço. Espaços no meio seguem enviáveis e chegam à API.
_TOKEN_CHARS = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?")


@dataclass(frozen=True)
class Profile:
    api_url: str
    token: str | None

    def __repr__(self) -> str:
        # O token nunca aparece numa representação.
        token = "<definido>" if self.token else None
        return f"Profile(api_url={_display_url(self.api_url)!r}, token={token!r})"


class ProfileError(Exception):
    """Perfil inutilizável. `reason` é o motivo fixo, em português, sem dados do usuário."""

    reason = "o perfil é inválido."

    def __init__(self) -> None:
        super().__init__(self.reason)


class InvalidProfileFile(ProfileError):
    """TOML malformado, ou com `api_url`/`token` de tipo errado."""

    reason = "o arquivo de perfil não é um TOML válido."


class UnreadableProfileFile(ProfileError):
    """O arquivo de perfil existe, mas não pôde ser lido (permissão, diretório no lugar etc.)."""

    reason = "não foi possível ler o arquivo de perfil."


class InvalidApiUrl(ProfileError):
    """O endereço efetivo da API não é `http://` ou `https://` com host, ou tem um host que o
    transporte não consegue codificar (rótulo vazio, IDNA inválido)."""

    reason = "o endereço da API é inválido."


class InvalidToken(ProfileError):
    """O token efetivo tem caracteres que não cabem num cabeçalho `Authorization: Bearer`, ou
    começa ou termina com espaço (ou só tem espaços)."""

    reason = "o token do perfil é inválido."


def default_config_path(env: Mapping[str, str]) -> pathlib.Path:
    """`CQ_CONFIG` do `env` (se não vazio) ou `Path(click.get_app_dir("cq")) / "config.toml"`.

    O diretório padrão vem do ambiente do processo (`APPDATA`, `XDG_CONFIG_HOME`), não do `env`.
    """
    custom = _non_empty(env.get("CQ_CONFIG"))
    if custom is not None:
        return pathlib.Path(custom)
    # Import preguiçoso: a camada sem Click importa este módulo sem carregar o Click.
    import click

    return pathlib.Path(click.get_app_dir(APP_NAME)) / CONFIG_FILE_NAME


def load_profile(env: Mapping[str, str]) -> Profile:
    """Resolve o perfil. Levanta `InvalidProfileFile`, `UnreadableProfileFile`, `InvalidApiUrl` ou
    `InvalidToken`."""
    stored = _read_profile_file(default_config_path(env))
    api_url = _non_empty(env.get("CQ_API_URL")) or stored.get("api_url") or DEFAULT_API_URL
    token = _non_empty(env.get("CQ_TOKEN")) or stored.get("token") or None
    if not is_valid_api_url(api_url):
        raise InvalidApiUrl()
    if token is not None and not _TOKEN_CHARS.fullmatch(token):
        raise InvalidToken()
    return Profile(api_url=api_url, token=token)


def is_valid_api_url(url: str) -> bool:
    """`http://` ou `https://` com host (e porta válida, se houver) que o transporte aceita."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 - valida a porta (levanta ValueError se inválida)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    return _transport_accepts(url)


def _transport_accepts(url: str) -> bool:
    """O endereço que o `ApiClient` monta passa pela codificação do transporte.

    Duas etapas, as mesmas do pedido: o `httpx2.URL` (que codifica em IDNA um host não ASCII e
    recusa, por exemplo, `☃.com`) e a codificação `idna` que a resolução de nomes aplica ao host
    (`socket.getaddrinfo`), que recusa rótulo vazio (`a..b`) ou com mais de 63 caracteres. Sem isso,
    esses hosts passam pelo `urlsplit` e só quebram dentro do httpx2, fora dos erros de transporte.
    """
    try:
        host = httpx2.URL(clean_base_url(url)).raw_host
        host.decode("ascii").encode("idna")
    except (httpx2.InvalidURL, UnicodeError, ValueError):
        return False
    return bool(host)


def _load_toml(path: pathlib.Path) -> dict[str, Any]:
    """O arquivo de perfil inteiro; `{}` se ele não existe. Erros fixos, sem o conteúdo."""
    try:
        with path.open("rb") as fp:
            return tomllib.load(fp)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise InvalidProfileFile() from None
    except OSError:
        raise UnreadableProfileFile() from None


def _read_profile_file(path: pathlib.Path) -> dict[str, str]:
    """Campos do arquivo de perfil; `{}` se ele não existe."""
    return _top_fields(_load_toml(path))


def _top_fields(data: Mapping[str, Any]) -> dict[str, str]:
    """`api_url` e `token` do topo (o perfil implícito): tipo errado é erro; vazio vale como
    ausente."""
    fields: dict[str, str] = {}
    for key in ("api_url", "token"):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str):
            raise InvalidProfileFile()
        if value:
            fields[key] = value
    return fields


def _non_empty(value: str | None) -> str | None:
    return value if value else None


def _display_url(url: str) -> str:
    """O endereço sem credenciais (`userinfo`), para representações."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<inválido>"
    netloc = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


# --------------------------------------------------------------------------------------------------
# Perfis nomeados: tipos, constantes e erros; depois, `resolve_profile` e
# `insecure_transport_warning`.
# --------------------------------------------------------------------------------------------------

ProfileKind = Literal["local", "remote"]

# O projeto Compose padrão, os reservados (a demonstração e os testes de sistema) e
# o formato aceito (**[I]** a conferir na documentação do Compose).
DEFAULT_PROJECT = "cq"
RESERVED_PROJECTS = frozenset({"cq-demo", "cq-systest"})
PROJECT_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"

# As chaves aceitas num perfil nomeado.
PROFILE_KEYS = frozenset({"kind", "api_url", "token", "stack_dir", "compose_project"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# O aviso de `http://` (stderr); `{name}` passa pelo `_terminal_safe` do CLI.
INSECURE_TRANSPORT_TEMPLATE = 'Aviso: o perfil "{name}" usa http:// fora do loopback; o token vai sem cifragem.'


class UnknownProfile(ProfileError):
    """O perfil escolhido (`--profile`, `CQ_PROFILE` ou `default_profile`) não existe em `profiles`."""

    reason = "o perfil escolhido não existe."


class UnknownProfileKey(ProfileError):
    """Um perfil nomeado tem uma chave fora de `PROFILE_KEYS`."""

    reason = "o perfil tem uma chave desconhecida."


class InvalidProfileField(ProfileError):
    """`stack_dir` ou `compose_project` num perfil cujo tipo (declarado ou inferido) é `remote`."""

    reason = "stack_dir e compose_project só valem no perfil local."


class InvalidProfileKind(ProfileError):
    """`kind` diferente de `local` e `remote`."""

    reason = "o tipo do perfil deve ser local ou remote."


class InvalidComposeProject(ProfileError):
    """`compose_project` fora do `PROJECT_PATTERN` ou entre os `RESERVED_PROJECTS`."""

    reason = "o nome do projeto Compose do perfil é inválido ou reservado."


@dataclass(frozen=True)
class EffectiveProfile:
    """O perfil efetivo. `name = None` é o perfil implícito; `origins` diz de
    onde veio cada campo (chaves `name`, `kind`, `api_url`, `token`, `compose_project`)."""

    name: str | None
    kind: ProfileKind
    api_url: str
    token: str | None
    stack_dir: pathlib.Path | None
    compose_project: str
    origins: Mapping[str, str]
    named: bool
    config_path: pathlib.Path

    @property
    def display_url(self) -> str:
        """O `api_url` sem `user:senha@` (a regra de `_display_url`): o único endereço que vai a
        texto, JSON e aviso."""
        return _display_url(self.api_url)

    def as_profile(self) -> Profile:
        return Profile(api_url=self.api_url, token=self.token)

    def __repr__(self) -> str:
        # Sem o token e sem credenciais no endereço.
        token = "<definido>" if self.token else None
        return (f"EffectiveProfile(name={self.name!r}, kind={self.kind!r}, "
                f"api_url={self.display_url!r}, token={token!r}, stack_dir={self.stack_dir!r}, "
                f"compose_project={self.compose_project!r}, origins={dict(self.origins)!r}, "
                f"named={self.named!r}, config_path={self.config_path!r})")


def resolve_profile(env: Mapping[str, str], selector: str | None = None) -> EffectiveProfile:
    """Resolve o perfil efetivo (nos passos 1 a 6 abaixo). Levanta os `ProfileError` do perfil implícito e
    `UnknownProfile`, `UnknownProfileKey`, `InvalidProfileField`, `InvalidProfileKind` ou
    `InvalidComposeProject`.

    Nenhum erro ecoa o nome escolhido, o conteúdo do arquivo, o endereço nem o token: todos levam só
    o motivo fixo da classe."""
    # Passo 1: o arquivo (ausente = vazio), com as chaves de topo e todos os perfis nomeados
    # validados aqui, antes de qualquer variável (o tipo de cada perfil é o dele próprio).
    config_path = default_config_path(env)
    data = _load_toml(config_path)
    top = _top_fields(data)
    default_profile = data.get("default_profile")
    if default_profile is not None and not isinstance(default_profile, str):
        raise InvalidProfileFile()
    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise InvalidProfileFile()
    profiles = {name: _parse_named_profile(fields) for name, fields in raw_profiles.items()}

    # Passo 2: o nome escolhido (a opção > `CQ_PROFILE` > `default_profile`).
    origins: dict[str, str] = {}
    if selector is not None:
        name: str | None = selector
        origins["name"] = "option"
    elif _non_empty(env.get("CQ_PROFILE")) is not None:
        name = env["CQ_PROFILE"]
        origins["name"] = "env"
    elif default_profile:
        name = default_profile
        origins["name"] = "file"
    else:
        name = None
        origins["name"] = "default"

    # Passo 3: o perfil nomeado ou, sem nome, o implícito (as chaves de topo só formam o implícito).
    if name is None:
        stored, field_origin = top, "file"
        declared_kind: str | None = None
        stack_dir: pathlib.Path | None = None
        project: str | None = None
    else:
        if name not in profiles:
            raise UnknownProfile()
        named = profiles[name]
        stored, field_origin = named.fields, "profile"
        declared_kind, stack_dir, project = named.kind, named.stack_dir, named.compose_project

    # Passo 4: `CQ_API_URL`/`CQ_TOKEN` não vazias vencem o campo correspondente.
    api_url, origins["api_url"] = _pick(env, "CQ_API_URL", stored, "api_url", field_origin)
    token, origins["token"] = _pick(env, "CQ_TOKEN", stored, "token", field_origin)
    api_url = api_url or DEFAULT_API_URL
    # As mesmas validações do `load_profile`, sobre os valores efetivos.
    if not is_valid_api_url(api_url):
        raise InvalidApiUrl()
    if token is not None and not _TOKEN_CHARS.fullmatch(token):
        raise InvalidToken()

    # Passo 5: o tipo declarado, ou inferido do host do endereço efetivo.
    if declared_kind is not None:
        kind: ProfileKind = "local" if declared_kind == "local" else "remote"
        origins["kind"] = "profile"
    else:
        kind = _inferred_kind(api_url)
        origins["kind"] = "inferred"

    # Passo 6: o projeto Compose do perfil, ou o padrão.
    if project is not None:
        origins["compose_project"] = "profile"
    else:
        project = DEFAULT_PROJECT
        origins["compose_project"] = "default"

    return EffectiveProfile(name=name, kind=kind, api_url=api_url, token=token, stack_dir=stack_dir,
                            compose_project=project,
                            origins={key: origins[key] for key in _ORIGIN_KEYS},
                            named=name is not None, config_path=config_path)


def insecure_transport_warning(profile: EffectiveProfile) -> str | None:
    """O aviso de `http://`: só com perfil nomeado, `remote`, esquema `http` e host
    fora do loopback; senão `None`."""
    if not profile.named or profile.kind != "remote":
        return None
    try:
        parts = urllib.parse.urlsplit(profile.api_url)
    except ValueError:
        return None
    if parts.scheme != "http" or _is_loopback(parts.hostname):
        return None
    return INSECURE_TRANSPORT_TEMPLATE.format(name=profile.name)


_ORIGIN_KEYS = ("name", "kind", "api_url", "token", "compose_project")


@dataclass(frozen=True)
class _NamedProfile:
    """Um perfil nomeado já validado (passo 1): `fields` com `api_url`/`token` não vazios."""

    fields: dict[str, str]
    kind: str | None
    stack_dir: pathlib.Path | None
    compose_project: str | None


def _parse_named_profile(raw: Any) -> _NamedProfile:
    """Valida `profiles.<nome>` pelo tipo do próprio perfil (declarado, ou inferido do `api_url` dele;
    sem `api_url`, o padrão), sem olhar as variáveis de ambiente."""
    if not isinstance(raw, dict):
        raise InvalidProfileFile()
    if any(key not in PROFILE_KEYS for key in raw):
        raise UnknownProfileKey()
    if any(not isinstance(value, str) for value in raw.values()):
        raise InvalidProfileFile()
    kind = raw.get("kind")
    if kind is not None and kind not in ("local", "remote"):
        raise InvalidProfileKind()
    fields = {key: raw[key] for key in ("api_url", "token") if raw.get(key)}
    own_kind = kind if kind is not None else _inferred_kind(fields.get("api_url", DEFAULT_API_URL))
    if own_kind == "remote" and ("stack_dir" in raw or "compose_project" in raw):
        raise InvalidProfileField()
    project = raw.get("compose_project")
    if project is not None and (not re.fullmatch(PROJECT_PATTERN, project)
                                or project in RESERVED_PROJECTS):
        raise InvalidComposeProject()
    # `stack_dir` como configurado, sem descoberta nem verificação de existência; vazio vale
    # como ausente, como os demais textos do perfil.
    stack_dir = pathlib.Path(raw["stack_dir"]) if raw.get("stack_dir") else None
    return _NamedProfile(fields=fields, kind=kind, stack_dir=stack_dir, compose_project=project)


def _pick(env: Mapping[str, str], variable: str, stored: Mapping[str, str], key: str,
          stored_origin: str) -> tuple[str | None, str]:
    """O valor de um campo e a sua origem: a variável não vazia, o campo guardado ou nenhum."""
    from_env = _non_empty(env.get(variable))
    if from_env is not None:
        return from_env, "env"
    if stored.get(key):
        return stored[key], stored_origin
    return None, "default"


def _inferred_kind(api_url: str) -> ProfileKind:
    """`local` se o host for `127.0.0.1`, `::1` ou `localhost`; `remote` nos demais."""
    try:
        host = urllib.parse.urlsplit(api_url).hostname
    except ValueError:
        return "remote"
    return "local" if _is_loopback(host) else "remote"


def _is_loopback(host: str | None) -> bool:
    return host is not None and host in LOOPBACK_HOSTS
