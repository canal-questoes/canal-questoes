"""Cliente da API HTTP v1.

Um método por operação do catálogo, que devolve o corpo JSON da resposta (`dict`). Um argumento
`None` não é enviado. Erros:
- `ApiError(status, body)` em 4xx/5xx (e em qualquer resposta fora de 2xx), com o corpo de erro da
  API; `health` devolve o corpo também no 503;
- `ApiUnreachable(url)` em qualquer erro de transporte, com o `base_url` sem barra final e sem
  credenciais.

O cliente HTTP pode ser injetado (`http=`, qualquer objeto com a interface de `httpx2.Client`, como o
`TestClient` da API). Sem ele, o cliente cria um `httpx2.Client` próprio, com timeout de conexão de
5 s, e o fecha em `close()`. As URLs são sempre absolutas, montadas sobre o `base_url`.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

import httpx2

CLIENT_KINDS = ("cli", "mcp", "other")
CONNECT_TIMEOUT_SECONDS = 5.0
# Leitura, escrita e pool: generoso, porque a API só responde depois de gravar (nenhuma resposta perdida).
OTHER_TIMEOUT_SECONDS = 30.0

UNEXPECTED_RESPONSE_CODE = "unexpected_response"


class ApiError(Exception):
    """A API recusou o pedido (4xx/5xx). `body` é o envelope de erro."""

    def __init__(self, status: int, body: dict) -> None:
        super().__init__(status)
        self.status = status
        self.body = body

    @property
    def code(self) -> str | None:
        error = self.body.get("error") if isinstance(self.body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) else None

    @property
    def pending(self) -> dict | None:
        """O objeto `pending` do envelope (`purge_pending`), ou `None` sem ele."""
        error = self.body.get("error") if isinstance(self.body, dict) else None
        pending = error.get("pending") if isinstance(error, dict) else None
        return pending if isinstance(pending, dict) else None

    def __repr__(self) -> str:
        return f"ApiError(status={self.status!r}, code={self.code!r})"


class ApiUnreachable(Exception):
    """Não houve conexão com a API. `url` é o `base_url` sem barra final e sem credenciais."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


def clean_base_url(url: str) -> str:
    """O endereço sem credenciais (`userinfo`), sem consulta e fragmento, e sem barra final."""
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


class ApiClient:
    """Cliente das operações do catálogo (`contracts/api/v1/catalog.json`)."""

    def __init__(self, base_url: str, token: str | None, *, client_kind: str = "cli",
                 http: Any | None = None) -> None:
        if client_kind not in CLIENT_KINDS:
            raise ValueError(f"client_kind deve ser um de {CLIENT_KINDS}")
        # As credenciais do endereço nunca são enviadas nem mostradas: a API autentica por token.
        self.base_url = clean_base_url(base_url)
        self._token = token or None
        self._client_kind = client_kind
        if http is None:
            self._http: Any = httpx2.Client(
                timeout=httpx2.Timeout(OTHER_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS))
            self._owns_http = True
        else:
            self._http = http
            self._owns_http = False

    def __repr__(self) -> str:
        token = "<definido>" if self._token else None
        return f"ApiClient(base_url={self.base_url!r}, token={token!r}, client_kind={self._client_kind!r})"

    # --- ciclo de vida -------------------------------------------------------------------------

    def close(self) -> None:
        """Fecha o cliente HTTP próprio (o injetado fica com quem o criou). Idempotente."""
        if self._owns_http:
            self._owns_http = False
            self._http.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- operações do catálogo -----------------------------------------------------------------

    def health(self) -> dict:
        """`system.health` — `GET /health`; o corpo também no 503."""
        return self._call("GET", "/health", accept_statuses=(200, 503), authenticate=False)

    def register_person(self, *, name: str, ra: str | None = None, email: str | None = None) -> dict:
        """`people.register` — `POST /v1/people`."""
        return self._call("POST", "/v1/people", json=_drop_none({"name": name, "ra": ra, "email": email}))

    def list_people(self, *, after: int | None = None, limit: int | None = None) -> dict:
        """`people.list` — `GET /v1/people`."""
        return self._call("GET", "/v1/people", params=_drop_none({"after": after, "limit": limit}))

    def get_person(self, ref: str) -> dict:
        """`people.get` — `GET /v1/people/{ref}`."""
        return self._call("GET", f"/v1/people/{_segment(ref)}")

    def reveal(self, pseudonyms: Sequence[str], *, reason: str) -> dict:
        """`identity.reveal` — `POST /v1/identity/reveal`."""
        return self._call("POST", "/v1/identity/reveal",
                          json={"pseudonyms": list(pseudonyms), "reason": reason})

    def create_token(self, *, label: str, permissions: Sequence[str]) -> dict:
        """`tokens.create` — `POST /v1/tokens`. O corpo traz o segredo: não o registre."""
        return self._call("POST", "/v1/tokens", json={"label": label, "permissions": list(permissions)})

    def list_tokens(self) -> dict:
        """`tokens.list` — `GET /v1/tokens`."""
        return self._call("GET", "/v1/tokens")

    def revoke_token(self, token_id: str) -> dict:
        """`tokens.revoke` — `POST /v1/tokens/{token_id}/revoke`."""
        return self._call("POST", f"/v1/tokens/{_segment(token_id)}/revoke")

    def list_events(self, types: Sequence[str], *, after: int | None = None,
                    limit: int | None = None, person: str | None = None) -> dict:
        """`events.list` — `GET /v1/events`.

        `person` (pseudônimo ou apelido) filtra pelos campos de pessoa; com
        `person=None`, o pedido leva só os argumentos dados."""
        params: list[tuple[str, Any]] = [("types", t) for t in types]
        params += list(_drop_none({"after": after, "limit": limit, "person": person}).items())
        return self._call("GET", "/v1/events", params=params)

    # --- oferecimentos ----------------------------------------------------------------------------

    def create_offering(self, *, code: str, course: str, term: str, timezone: str,
                        response_window_minutes: int | None = None, free_messages: bool | None = None,
                        contact_email: str | None = None) -> dict:
        """`offerings.create` — `POST /v1/offerings`.

        `free_messages` e `contact_email` só vão no corpo quando dados (ausentes, a API usa
        desligado e sem contato)."""
        return self._call("POST", "/v1/offerings", json=_drop_none({
            "code": code, "course": course, "term": term, "timezone": timezone,
            "response_window_minutes": response_window_minutes, "free_messages": free_messages,
            "contact_email": contact_email}))

    def update_offering_settings(self, ref: str, *, free_messages: bool | None = None,
                                 contact_email: str | None = None, clear_contact_email: bool = False) -> dict:
        """`offerings.update_settings` — `PATCH /v1/offerings/{ref}/settings`.

        Só vão no corpo os campos dados; `clear_contact_email=True` manda `contact_email: null` (remove o
        contato) e não combina com `contact_email`."""
        if clear_contact_email and contact_email is not None:
            raise ValueError("contact_email e clear_contact_email não combinam")
        body: dict[str, Any] = _drop_none({"free_messages": free_messages, "contact_email": contact_email})
        if clear_contact_email:
            body["contact_email"] = None
        return self._call("PATCH", _offering_path(ref, "/settings"), json=body)

    def list_offerings(self, *, after: int | None = None, limit: int | None = None) -> dict:
        """`offerings.list` — `GET /v1/offerings`."""
        return self._call("GET", "/v1/offerings", params=_drop_none({"after": after, "limit": limit}))

    def get_offering(self, ref: str) -> dict:
        """`offerings.get` — `GET /v1/offerings/{ref}`."""
        return self._call("GET", _offering_path(ref))

    def close_offering(self, ref: str) -> dict:
        """`offerings.close` — `POST /v1/offerings/{ref}/close`."""
        return self._call("POST", _offering_path(ref, "/close"))

    def prepare_archive(self, ref: str) -> dict:
        """`offerings.prepare_archive` — `POST /v1/offerings/{ref}/archive/prepare`."""
        return self._call("POST", _offering_path(ref, "/archive/prepare"))

    def archive_offering(self, ref: str, *, confirmation_code: str) -> dict:
        """`offerings.archive` — `POST /v1/offerings/{ref}/archive`."""
        return self._call("POST", _offering_path(ref, "/archive"),
                          json={"confirmation_code": confirmation_code})

    # --- diagnóstico, expurgo, bots e equipe ------------------------------------------------------

    def system_doctor(self) -> dict:
        """`system.doctor` — `GET /v1/system/doctor` (sem consulta nem corpo)."""
        return self._call("GET", "/v1/system/doctor")

    def prepare_purge(self, ref: str) -> dict:
        """`offerings.prepare_purge` — `POST /v1/offerings/{ref}/purge/prepare` (sem corpo)."""
        return self._call("POST", _offering_path(ref, "/purge/prepare"))

    def purge_offering(self, ref: str, *, confirmation_code: str) -> dict:
        """`offerings.purge` — `POST /v1/offerings/{ref}/purge`."""
        return self._call("POST", _offering_path(ref, "/purge"),
                          json={"confirmation_code": confirmation_code})

    def register_bot(self, ref: str, *, token: str) -> dict:
        """`bots.register` — `PUT /v1/offerings/{ref}/bot`. O pedido traz o token: não o registre.

        O token vai só no corpo, nunca na URL nem num cabeçalho.
        """
        return self._call("PUT", _offering_path(ref, "/bot"), json={"token": token})

    def activate_bot(self, ref: str) -> dict:
        """`bots.activate` — `POST /v1/offerings/{ref}/bot/activate`."""
        return self._call("POST", _offering_path(ref, "/bot/activate"))

    def get_bot(self, ref: str) -> dict:
        """`bots.get` — `GET /v1/offerings/{ref}/bot`."""
        return self._call("GET", _offering_path(ref, "/bot"))

    def create_team_link(self, ref: str, *, person: str, valid_hours: int | None = None) -> dict:
        """`team.link` — `POST /v1/offerings/{ref}/team-links`. O corpo traz o link: não o registre."""
        return self._call("POST", _offering_path(ref, "/team-links"),
                          json=_drop_none({"person": person, "valid_hours": valid_hours}))

    # --- alunos e convites -------------------------------------------------------------------------

    def import_students(self, ref: str, *, rows: Sequence[Mapping[str, str]]) -> dict:
        """`students.import` — `POST /v1/offerings/{ref}/students`. O pedido traz nome, RA e e-mail:
        não o registre.

        As linhas vão como estão, na ordem dada e sem aparar: as regras de normalização ficam
        na API.
        """
        return self._call("POST", _offering_path(ref, "/students"),
                          json={"rows": [dict(row) for row in rows]})

    def get_students_summary(self, ref: str) -> dict:
        """`students.summary` — `GET /v1/offerings/{ref}/students/summary`."""
        return self._call("GET", _offering_path(ref, "/students/summary"))

    def prepare_send_invites(self, ref: str, *, valid_hours: int | None = None) -> dict:
        """`invites.prepare_send` — `POST /v1/offerings/{ref}/invites/prepare`.

        O corpo é sempre um objeto: `{}` quando `valid_hours` é `None`.
        """
        return self._call("POST", _offering_path(ref, "/invites/prepare"),
                          json=_drop_none({"valid_hours": valid_hours}))

    def send_invites(self, ref: str, *, confirmation_code: str) -> dict:
        """`invites.send` — `POST /v1/offerings/{ref}/invites`."""
        return self._call("POST", _offering_path(ref, "/invites"),
                          json={"confirmation_code": confirmation_code})

    def resend_invites(self, ref: str, *, pseudonyms: Sequence[str],
                       valid_hours: int | None = None) -> dict:
        """`invites.resend` — `POST /v1/offerings/{ref}/invites/resend`.

        A lista vai como foi dada, com os repetidos: o limite de 100 vale para a lista
        enviada.
        """
        return self._call("POST", _offering_path(ref, "/invites/resend"),
                          json=_drop_none({"pseudonyms": list(pseudonyms), "valid_hours": valid_hours}))

    # --- disparos e mensagens livres -----------------------------------------------------------

    def prepare_dispatch(self, ref: str, *, repo: str, commit: str, qid: str, opens_at: str | None = None,
                         closes_at: str | None = None, origin: str | None = None) -> dict:
        """`dispatches.prepare` — `POST /v1/offerings/{ref}/dispatches`.

        Os campos vão como estão (os horários com o deslocamento que vierem); sem `origin`, vale a do
        `X-CQ-Client`."""
        return self._call("POST", _offering_path(ref, "/dispatches"),
                          json=_drop_none({"repo": repo, "commit": commit, "qid": qid, "opens_at": opens_at,
                                           "closes_at": closes_at, "origin": origin}))

    def get_dispatch(self, ref: str, dispatch_id: str) -> dict:
        """`dispatches.get` — `GET /v1/offerings/{ref}/dispatches/{dispatch_id}`."""
        return self._call("GET", _offering_path(ref, f"/dispatches/{_segment(dispatch_id)}"))

    def list_dispatches(self, ref: str, *, after: int | None = None, limit: int | None = None) -> dict:
        """`dispatches.list` — `GET /v1/offerings/{ref}/dispatches`."""
        return self._call("GET", _offering_path(ref, "/dispatches"),
                          params=_drop_none({"after": after, "limit": limit}))

    def confirm_dispatch(self, ref: str, dispatch_id: str) -> dict:
        """`dispatches.confirm` — `POST /v1/offerings/{ref}/dispatches/{dispatch_id}/confirm`."""
        return self._call("POST", _offering_path(ref, f"/dispatches/{_segment(dispatch_id)}/confirm"))

    def cancel_dispatch(self, ref: str, dispatch_id: str) -> dict:
        """`dispatches.cancel` — `POST /v1/offerings/{ref}/dispatches/{dispatch_id}/cancel`."""
        return self._call("POST", _offering_path(ref, f"/dispatches/{_segment(dispatch_id)}/cancel"))

    def list_free_messages(self, ref: str, *, after: int | None = None, limit: int | None = None) -> dict:
        """`messages.list` — `GET /v1/offerings/{ref}/messages`."""
        return self._call("GET", _offering_path(ref, "/messages"),
                          params=_drop_none({"after": after, "limit": limit}))

    # --- relatórios e resumo diário ---------------------------------------------------------------

    def get_report(self, ref: str, dispatch_id: str, *, version: str | int | None = None) -> dict:
        """`reports.get` — `GET /v1/offerings/{ref}/dispatches/{dispatch_id}/report` (JSON, o padrão da
        API: o `format` não é enviado)."""
        return self._call("GET", _offering_path(ref, f"/dispatches/{_segment(dispatch_id)}/report"),
                          params=_drop_none({"version": version}))

    def get_report_csv(self, ref: str, dispatch_id: str, *, version: str | int | None = None) -> str:
        """`reports.get` com `format=csv`: o corpo `text/csv` como veio (pelo `_call_text`)."""
        return self._call_text("GET", _offering_path(ref, f"/dispatches/{_segment(dispatch_id)}/report"),
                               params=_drop_none({"format": "csv", "version": version}))

    def list_report_versions(self, ref: str, dispatch_id: str, *, after: int | None = None,
                             limit: int | None = None) -> dict:
        """`reports.list_versions` — `GET /v1/offerings/{ref}/dispatches/{dispatch_id}/report/versions`."""
        return self._call("GET", _offering_path(ref, f"/dispatches/{_segment(dispatch_id)}/report/versions"),
                          params=_drop_none({"after": after, "limit": limit}))

    def resend_report_summary(self, ref: str, dispatch_id: str) -> dict:
        """`reports.resend_summary` — `POST /v1/offerings/{ref}/dispatches/{dispatch_id}/report/resend` (sem
        corpo)."""
        return self._call("POST", _offering_path(ref, f"/dispatches/{_segment(dispatch_id)}/report/resend"))

    def get_offering_digest(self, ref: str) -> dict:
        """`offerings.get_digest` — `GET /v1/offerings/{ref}/digest`."""
        return self._call("GET", _offering_path(ref, "/digest"))

    def update_offering_digest(self, ref: str, *, enabled: bool | None = None,
                               local_time: str | None = None) -> dict:
        """`offerings.update_digest` — `PATCH /v1/offerings/{ref}/digest`.

        Só vão no corpo os campos dados; o `local_time` vai como veio (quem valida é a API)."""
        return self._call("PATCH", _offering_path(ref, "/digest"),
                          json=_drop_none({"enabled": enabled, "local_time": local_time}))

    # --- transporte ----------------------------------------------------------------------------

    def _call(self, method: str, path: str, *, params: Any = None, json: Any = None,
              accept_statuses: Sequence[int] | None = None, authenticate: bool = True) -> dict:
        headers = {"X-CQ-Client": self._client_kind}
        if authenticate and self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        kwargs: dict[str, Any] = {"headers": headers}
        if params:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        try:
            response = self._http.request(method, self.base_url + path, **kwargs)
        except httpx2.TransportError:
            # Sem a exceção original: a mensagem dela pode trazer o endereço com credenciais.
            raise ApiUnreachable(self.base_url) from None
        status = response.status_code
        body = _json_object(response)
        accepted = status in accept_statuses if accept_statuses is not None else 200 <= status < 300
        if accepted and body is not None:
            return body
        if accepted or body is None or not _is_error_envelope(body):
            body = {"error": {"code": UNEXPECTED_RESPONSE_CODE,
                              "message": f"Resposta inesperada da API (HTTP {status})."}}
        raise ApiError(status, body)

    def _call_text(self, method: str, path: str, *, params: Any = None) -> str:
        """O transporte de texto: só o 200 com `text/csv` é aceito, e o corpo volta decodificado em
        UTF-8 sem nenhuma mudança (com o BOM); os erros como no `_call` (o envelope da API, ou
        `unexpected_response`)."""
        headers = {"X-CQ-Client": self._client_kind}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        kwargs: dict[str, Any] = {"headers": headers}
        if params:
            kwargs["params"] = params
        try:
            response = self._http.request(method, self.base_url + path, **kwargs)
        except httpx2.TransportError:
            raise ApiUnreachable(self.base_url) from None
        status = response.status_code
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if status == 200 and media == "text/csv":
            try:
                return response.content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        body = None if status == 200 else _json_object(response)
        if body is None or not _is_error_envelope(body):
            body = {"error": {"code": UNEXPECTED_RESPONSE_CODE,
                              "message": f"Resposta inesperada da API (HTTP {status})."}}
        raise ApiError(status, body)


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _offering_path(ref: str, suffix: str = "") -> str:
    """Caminho de uma operação sobre o oferecimento `ref` (`offering_id` ou `code`, como texto)."""
    return f"/v1/offerings/{_segment(ref)}{suffix}"


def _segment(value: str) -> str:
    """Um segmento de caminho: o valor vai à API como texto, com `/`, `?`, `#` e `%` codificados.

    A codificação só vale no cliente. O servidor ASGI (o uvicorn, e também o `TestClient`)
    decodifica o caminho antes de rotear, e um `/` codificado (`%2F`) volta a separar segmentos:
    `people show a/b` cai como rota desconhecida, 404 `not_found`, e não como o 400
    `invalid_format` de um valor malformado. O servidor não resolve `.` e `..`, então o pedido não
    chega a outra operação; e, como nenhum pseudônimo ou `token_id` válido tem `/`, só muda o erro
    de um valor já inválido. `?` e `#` (`%3F`, `%23`) ficam no segmento e voltam como formato
    inválido.

    `.` e `..` saem com os pontos codificados (`%2E`): o `quote` os deixa como estão, e o httpx2
    remove os segmentos de ponto (RFC 3986, 5.2.4), o que mandaria `people show .` para
    `GET /v1/people`, outra operação. O `%2E` sai intacto (o httpx2 não o decodifica); o servidor o
    decodifica dentro do segmento, e a API recusa o `.` como formato inválido.
    """
    segment = urllib.parse.quote(str(value), safe="")
    if segment in (".", ".."):
        return "%2E" * len(segment)
    return segment


def _json_object(response: Any) -> dict | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _is_error_envelope(body: dict) -> bool:
    error = body.get("error")
    return isinstance(error, dict) and isinstance(error.get("message"), str)
