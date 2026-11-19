"""CLI da configuração de mensagens livres do oferecimento.

Cobre a folha `offerings settings` e as opções de mensagens livres de `offerings create`;
as regras do CLI (a linha de uso, a forma das folhas, os erros de uso sem chamada à API e o texto
para pessoas) e o corpo do `PATCH`; a operação e o CLI, sem mudar as outras linhas de oferecimentos (a
folha imprime `Configuração de <code>: mensagens livres <ligadas|desligadas>; contato do professor
<definido|não definido>.`).

O CLI roda em processo pelo `cli_invoke` do conftest (`CliRunner` com o `TestClient` da API em
`obj["http"]`); o `_Recorder` deste arquivo repassa ao `TestClient` e guarda cada pedido, para afirmar o
corpo exato que o CLI manda e que nenhum pedido sai nos erros de uso. O e-mail do contato nunca é impresso
(regra do CLI). Identificadores vêm da própria API.

Vermelho antes da implementação (testes primeiro): `offerings create` não tem
`--free-messages` nem `--contact-email` (erro de uso do `click`, código 2), e `offerings settings` não
existe; nos erros de uso, o controle positivo do mesmo teste (a folha com uma opção válida chama a API e
imprime a linha) é o que fica vermelho, porque um comando inexistente também termina com código 2.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import pytest

from tests.helpers.api import ADMIN_TOKEN, auth, create_offering
from tests.helpers.contract import assert_matches

ADMIN = auth(ADMIN_TOKEN)
COURSE = "Algoritmos e Programação"
TERM = "2031s1"
TZ = "America/Sao_Paulo"

# Contatos de professor fictícios.
PROF = "prof.cli@docentes.example.org"
PROF_OTHER = "prof.cli.outra@docentes.example.org"


# --------------------------------------------------------------------------------------------------
# Apoio


class _Recorder:
    """Cliente HTTP que repassa ao `TestClient` e guarda cada pedido (método, caminho e corpo JSON)."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.requests: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.requests.append((method, urllib.parse.urlsplit(url).path, kwargs.get("json")))
        return self._client.request(method, url, **kwargs)


def _out(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"exceção não tratada no CLI: {result.exception!r}")
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _ok(result: Any) -> None:
    _no_traceback(result)
    assert result.exit_code == 0, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stderr == ""


def _usage(result: Any) -> None:
    """Erro de uso: código 2, nada na saída padrão, `Uso incorreto: …`."""
    _no_traceback(result)
    assert result.exit_code == 2, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""
    assert result.stderr.startswith("Uso incorreto: "), result.stderr


def _api_get(client: Any, ref: str) -> dict:
    resp = client.get(f"/v1/offerings/{ref}", headers=ADMIN)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.get", 200, body)
    return body


def _line(code: str, *, on: bool, contact: bool) -> str:
    """A linha de `offerings settings` (texto para pessoas)."""
    return (f"Configuração de {code}: mensagens livres {'ligadas' if on else 'desligadas'}; "
            f"contato do professor {'definido' if contact else 'não definido'}.")


def _settings(ref: str) -> str:
    return f"/v1/offerings/{ref}/settings"


def _no_email(result: Any, *emails: str) -> None:
    for email in emails:
        assert email not in result.stdout and email not in result.stderr, "o CLI imprimiu o e-mail do contato"


# --------------------------------------------------------------------------------------------------
# offerings create com as opções de mensagens livres


def test_offerings_create_with_settings(cli_invoke: Any, client: Any) -> None:
    """Contrato (`cq offerings create CODE --course C --term T --timezone Z
    [--response-window N] [--free-messages on|off] [--contact-email E]`; as linhas de `offerings create` e `offerings
    show` não mudam com as opções; o `--json` sai como a API): com `--free-messages on` e
    `--contact-email E`, o CLI manda o corpo exato sem as opções, mais `free_messages: true` e `contact_email: E`; a
    linha de texto é a mesma; o oferecimento chega à API com `true`/`true`; o e-mail não é impresso. Com
    `--free-messages off`, `free_messages: false` e nenhum contato; com `--json`, a saída é o `Offering` da
    API (sem o e-mail); sem as opções, o corpo não tem os campos."""
    http = _Recorder(client)
    result = cli_invoke(["offerings", "create", "cfg-cli-1", "--course", COURSE, "--term", TERM, "--timezone", TZ,
                         "--free-messages", "on", "--contact-email", PROF], http=http)
    _ok(result)
    assert http.requests == [("POST", "/v1/offerings", {
        "code": "cfg-cli-1", "course": COURSE, "term": TERM, "timezone": TZ, "free_messages": True,
        "contact_email": PROF})]
    api = _api_get(client, "cfg-cli-1")
    assert (api["free_messages"], api["contact_email_set"]) == (True, True)
    assert result.stdout == _out(f"Oferecimento cadastrado: cfg-cli-1 ({api['offering_id']}), em rascunho.")
    _no_email(result, PROF)

    http = _Recorder(client)
    off = cli_invoke(["offerings", "create", "cfg-cli-2", "--course", COURSE, "--term", TERM, "--timezone", TZ,
                      "--response-window", "90", "--free-messages", "off"], json=True, http=http)
    _ok(off)
    assert http.requests == [("POST", "/v1/offerings", {
        "code": "cfg-cli-2", "course": COURSE, "term": TERM, "timezone": TZ, "response_window_minutes": 90,
        "free_messages": False})]
    api_off = _api_get(client, "cfg-cli-2")
    assert (api_off["free_messages"], api_off["contact_email_set"]) == (False, False)
    assert json.loads(off.stdout) == api_off

    http = _Recorder(client)
    contact_json = cli_invoke(["offerings", "create", "cfg-cli-3", "--course", COURSE, "--term", TERM,
                               "--timezone", TZ, "--contact-email", PROF_OTHER], json=True, http=http)
    _ok(contact_json)
    assert http.requests == [("POST", "/v1/offerings", {
        "code": "cfg-cli-3", "course": COURSE, "term": TERM, "timezone": TZ, "contact_email": PROF_OTHER})]
    api_contact = _api_get(client, "cfg-cli-3")
    assert json.loads(contact_json.stdout) == api_contact
    assert (api_contact["free_messages"], api_contact["contact_email_set"]) == (False, True)
    _no_email(contact_json, PROF_OTHER)

    http = _Recorder(client)
    plain = cli_invoke(["offerings", "create", "cfg-cli-4", "--course", COURSE, "--term", TERM, "--timezone", TZ],
                       http=http)
    _ok(plain)
    assert http.requests == [("POST", "/v1/offerings", {
        "code": "cfg-cli-4", "course": COURSE, "term": TERM, "timezone": TZ})]


# --------------------------------------------------------------------------------------------------
# offerings settings


def test_offerings_settings_text_and_json(cli_invoke: Any, client: Any) -> None:
    """Contrato (`cq offerings settings REF [--free-messages on|off]
    [--contact-email E | --clear-contact-email]`; a linha `Configuração de <code>: mensagens livres
    <ligadas|desligadas>; contato do professor <definido|não definido>.`; o `--json` sai como a API) e o corpo
    do `PATCH`: `--free-messages off` → `{"free_messages": false}`; `--contact-email E` →
    `{"contact_email": E}`; `--free-messages on` com `--json` → `{"free_messages": true}` e a saída igual ao
    `Offering` da API; `--clear-contact-email` → `{"contact_email": null}`; as duas opções juntas → um corpo
    só, com os dois campos; o `REF` pelo identificador manda o caminho com ele, e a linha usa o código; a
    linha reflete a configuração da resposta, e o e-mail nunca é impresso."""
    offering = create_offering(client, ADMIN_TOKEN, code="cfg-cli-settings", timezone=TZ)
    code = offering["code"]

    steps = [
        (["--free-messages", "off"], {"free_messages": False}, (False, False)),
        (["--contact-email", PROF], {"contact_email": PROF}, (False, True)),
        (["--clear-contact-email"], {"contact_email": None}, (False, False)),
        (["--free-messages", "on", "--contact-email", PROF_OTHER],
         {"free_messages": True, "contact_email": PROF_OTHER}, (True, True)),
    ]
    for options, body, (on, contact) in steps:
        http = _Recorder(client)
        result = cli_invoke(["offerings", "settings", code, *options], http=http)
        _ok(result)
        assert http.requests == [("PATCH", _settings(code), body)], options
        assert result.stdout == _out(_line(code, on=on, contact=contact)), options
        _no_email(result, PROF, PROF_OTHER)
        api = _api_get(client, code)
        assert (api["free_messages"], api["contact_email_set"]) == (on, contact), options

    http = _Recorder(client)
    as_json = cli_invoke(["offerings", "settings", code, "--free-messages", "off"], json=True, http=http)
    _ok(as_json)
    assert http.requests == [("PATCH", _settings(code), {"free_messages": False})]
    printed = json.loads(as_json.stdout)
    assert_matches("offerings.update_settings", 200, printed)
    assert printed == _api_get(client, code)
    assert (printed["free_messages"], printed["contact_email_set"]) == (False, True)
    _no_email(as_json, PROF, PROF_OTHER)

    by_id = offering["offering_id"]
    http = _Recorder(client)
    result = cli_invoke(["offerings", "settings", by_id, "--clear-contact-email"], http=http)
    _ok(result)
    assert http.requests == [("PATCH", _settings(by_id), {"contact_email": None})]
    assert result.stdout == _out(_line(code, on=False, contact=False))


USAGE_CASES = [
    pytest.param([], id="sem-opcao"),
    pytest.param(["--contact-email", PROF, "--clear-contact-email"], id="contato-e-remocao-juntos"),
    pytest.param(["--free-messages", "talvez"], id="free-messages-fora-da-escolha"),
]


@pytest.mark.parametrize("options", USAGE_CASES)
def test_offerings_settings_usage_errors_make_no_call(options: list[str], cli_invoke: Any, client: Any) -> None:
    """Regra (`offerings settings` sem nenhuma das opções, ou com `--contact-email` e
    `--clear-contact-email` juntos: erro de uso (código 2), sem chamada à API; `--free-messages` é escolha
    `on`/`off`) e a forma geral (uso incorreto: código 2, `Uso incorreto: …`, sem eco): com e sem `--json`,
    código 2, nada na saída padrão, nenhum pedido à API e o oferecimento sem mudança; o e-mail não é
    impresso. Controle positivo (o que fica vermelho sem o comando): a mesma folha com
    `--free-messages on` chama a API uma vez e imprime a linha."""
    offering = create_offering(client, ADMIN_TOKEN, code="cfg-cli-uso", timezone=TZ)
    code = offering["code"]

    http = _Recorder(client)
    control = cli_invoke(["offerings", "settings", code, "--free-messages", "on"], http=http)
    _ok(control)
    assert http.requests == [("PATCH", _settings(code), {"free_messages": True})]
    assert control.stdout == _out(_line(code, on=True, contact=False))
    before = _api_get(client, code)

    for json_mode in (False, True):
        http = _Recorder(client)
        result = cli_invoke(["offerings", "settings", code, *options], json=json_mode, http=http)
        _usage(result)
        assert http.requests == [], (options, json_mode)
        _no_email(result, PROF)
    assert _api_get(client, code) == before
