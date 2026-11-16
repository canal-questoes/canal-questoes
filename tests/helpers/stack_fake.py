"""Dublê da interface da pilha, nível 1.


`FakeStack(timeline=[…])` cumpre o `Protocol` `cq_client.stack.Stack` sem Docker nem subprocesso:
- **instantes:** `timeline` é a lista dos `StackSnapshot` (ou dos nomes de `SNAPSHOTS`, montados por
  `snapshot(nome)`). Antes de `up()`, `snapshot()` devolve sempre o primeiro; `up()` passa ao segundo
  (se houver), e cada `snapshot()` depois de `up()` devolve o instante corrente e avança um; o último se
  repete. Assim, `[parado, subindo, saudável]` dá "parado" a toda leitura antes de subir e "subindo",
  "saudável", "saudável"… depois, qualquer que seja o número de leituras antes do `up()`. `down()` passa
  ao `after_down` (padrão: `snapshot("absent")`);
- **falhas por método:** `fail("up", StackFailure("port_in_use"))`, `fail("snapshot", DockerUnavailable())`,
  `fail("logs", KeyboardInterrupt(), after=2)` (o `after`, só no `logs`, dá as `after` primeiras linhas e
  então levanta); a falha vale para toda chamada até `clear_failure(método)`;
- `docker_available`, `compose_version` e `engine_image_id` configuráveis; `logs` com as linhas já limpas
  (lista, ou mapa serviço → linhas, filtrado pelos serviços pedidos);
- `calls`: o registro ordenado das chamadas, `(método, argumentos nomeados)`, inclusive as que falharam.

`FakeStack.unavailable()` é a pilha com o Docker indisponível (todo método de operação levanta
`DockerUnavailable`, `docker_available()` é falso, versões e imagem `None`).

`FakeSleep(clock)` é o `sleep` injetável: avança o `FakeClock` do teste e conta as esperas; `monotonic()`
é o relógio em segundos (`float`) sobre o mesmo `FakeClock`, para o `clock` injetável do `up`.

Endereços: `TEST_API_PORT` é a porta fixa dos casos de sucesso com
perfil local (diferente de 8000 e de 9), `TEST_API_URL` o `api_url` correspondente e `OTHER_API_PORT` a
porta divergente. Nenhuma delas escuta de verdade: a API dos testes chega por `http=` no `ctx.obj`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from cq_client.stack import DockerUnavailable, ServiceState, StackSnapshot

from tests.helpers.clock import FakeClock

TEST_API_PORT = 18765
OTHER_API_PORT = 18766
TEST_API_URL = f"http://127.0.0.1:{TEST_API_PORT}"

ENGINE_IMAGE_ID = "sha256:" + "3f9a1c0d7e5b" + "8" * 52
COMPOSE_VERSION = (2, 27, 0)

# Guarda contra laço sem fim no `up` (3600 s no limite com uma leitura por segundo cabem com folga).
MAX_SNAPSHOTS = 20000

_SERVICES = ("postgres", "api", "worker", "engine")


def _states(spec: Mapping[str, tuple[str, str]]) -> tuple[ServiceState, ...]:
    return tuple(ServiceState(s, *spec[s]) for s in _SERVICES)


_OK = {"postgres": ("running", "healthy"), "api": ("running", "healthy"), "worker": ("running", "none"),
       "engine": ("running", "healthy")}

# nome → (estados por serviço, a api publicada?, api_owned)
_SNAPSHOT_SPECS: dict[str, tuple[dict[str, tuple[str, str]], bool, bool | None]] = {
    # nunca criada (o `ps` vazio)
    "absent": ({s: ("absent", "none") for s in _SERVICES}, False, False),
    # parada (os contêineres existem, todos `exited`)
    "stopped": ({s: ("stopped", "none") for s in _SERVICES}, False, False),
    # um serviço saiu (o worker `exited`)
    "exited": ({**_OK, "worker": ("stopped", "none")}, True, True),
    # subindo (a saúde ainda `starting`)
    "starting": ({**_OK, "api": ("running", "starting"), "engine": ("running", "starting")}, True, True),
    # os quatro "ok" (o worker sem healthcheck: `none`)
    "healthy": (dict(_OK), True, True),
    # com falha (a api `unhealthy`)
    "unhealthy": ({**_OK, "api": ("running", "unhealthy")}, True, True),
    # laço de reinício (a api `restarting` → parada, com falha)
    "restart_loop": ({**_OK, "api": ("stopped", "unhealthy")}, False, False),
    # parcial (só o postgres no ar; o worker saiu; api e engine sem contêiner)
    "partial": ({"postgres": ("running", "healthy"), "api": ("absent", "none"),
                 "worker": ("stopped", "none"), "engine": ("absent", "none")}, False, False),
    # o Docker indisponível visto como estado ("não verificado")
    "unknown": ({s: ("unknown", "unknown") for s in _SERVICES}, False, None),
}
SNAPSHOTS = tuple(_SNAPSHOT_SPECS)


def snapshot(name: str, *, api_port: int | None = None, api_owned: bool | None | str = "default"
             ) -> StackSnapshot:
    """O `StackSnapshot` do instante `name` (`SNAPSHOTS`). A api publicada sai com `TEST_API_PORT`, ou
    com `api_port` (ex.: `OTHER_API_PORT` para a porta divergente); `api_owned` troca o padrão do instante
    (ex.: `snapshot("absent", api_owned=False)` com a API de outro projeto no endereço do perfil)."""
    states, published, owned = _SNAPSHOT_SPECS[name]
    port = (api_port if api_port is not None else TEST_API_PORT) if published else api_port
    return StackSnapshot(services=_states(states), api_port=port,
                         api_owned=owned if api_owned == "default" else api_owned)  # type: ignore[arg-type]


def healthy(*, api_port: int = TEST_API_PORT) -> StackSnapshot:
    return snapshot("healthy", api_port=api_port)


def port_mismatch() -> StackSnapshot:
    """Os quatro "ok", com a api publicada em `OTHER_API_PORT`."""
    return snapshot("healthy", api_port=OTHER_API_PORT)


def foreign_api() -> StackSnapshot:
    """A pilha do projeto ausente e a API do endereço do perfil de outro projeto (`api_owned` falso)."""
    return snapshot("absent", api_owned=False)


_DEFAULT_LOGS = (
    "api-1       | API no ar",
    "worker-1    | worker no ar",
    "postgres-1  | database system is ready to accept connections",
)

_METHODS = ("snapshot", "up", "down", "logs", "compose_version", "engine_image_id", "docker_available")


class FakeStack:
    """O `Stack` dublê (ver o docstring do módulo)."""

    def __init__(self, timeline: Sequence[StackSnapshot | str] | None = None, *,
                 docker_available: bool = True,
                 compose_version: tuple[int, int, int] | None = COMPOSE_VERSION,
                 engine_image_id: str | None = ENGINE_IMAGE_ID,
                 logs: Sequence[str] | Mapping[str, Sequence[str]] | None = None,
                 after_down: StackSnapshot | str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._failures: dict[str, tuple[BaseException | type[BaseException], int | None]] = {}
        self.set_timeline(timeline if timeline is not None else ["healthy"])
        self._docker_available = docker_available
        self._compose_version = compose_version
        self._engine_image_id = engine_image_id
        self._logs = logs if logs is not None else _DEFAULT_LOGS
        self._after_down = self._resolve(after_down if after_down is not None else "absent")
        self.snapshots_served = 0

    @classmethod
    def unavailable(cls) -> FakeStack:
        """O Docker indisponível: `docker_available()` falso, versões e imagem `None`, e `snapshot`, `up`,
        `down` e `logs` levantando `DockerUnavailable`."""
        stack = cls(["unknown"], docker_available=False, compose_version=None, engine_image_id=None)
        for method in ("snapshot", "up", "down", "logs"):
            stack.fail(method, DockerUnavailable())
        return stack

    # -- configuração ----------------------------------------------------------------------------

    @staticmethod
    def _resolve(item: StackSnapshot | str) -> StackSnapshot:
        return snapshot(item) if isinstance(item, str) else item

    def set_timeline(self, timeline: Sequence[StackSnapshot | str]) -> None:
        """Troca os instantes e volta ao primeiro (como antes de `up()`)."""
        items = [self._resolve(t) for t in timeline]
        if not items:
            raise ValueError("a linha do tempo precisa de ao menos um instante")
        self._timeline = items
        self._index = 0
        self._started = False

    def fail(self, method: str, exc: BaseException | type[BaseException], *, after: int | None = None) -> None:
        """Faz `method` levantar `exc` em toda chamada (no `logs`, com `after`, depois de `after` linhas)."""
        if method not in _METHODS:
            raise ValueError(f"método desconhecido: {method}")
        if after is not None and method != "logs":
            raise ValueError("after só vale para logs")
        self._failures[method] = (exc, after)

    def clear_failure(self, method: str) -> None:
        self._failures.pop(method, None)

    # -- consultas do teste --------------------------------------------------------------------

    def names(self) -> list[str]:
        """Os métodos chamados, na ordem."""
        return [name for name, _ in self.calls]

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)

    @property
    def current(self) -> StackSnapshot:
        """O instante que a próxima leitura devolveria."""
        return self._timeline[min(self._index, len(self._timeline) - 1)]

    # -- Protocol Stack --------------------------------------------------------------------------

    def _enter(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))
        failure = self._failures.get(method)
        if failure is not None and (failure[1] is None):
            raise failure[0]

    def snapshot(self) -> StackSnapshot:
        self._enter("snapshot")
        self.snapshots_served += 1
        if self.snapshots_served > MAX_SNAPSHOTS:
            raise AssertionError("FakeStack: leituras demais (laço sem fim no up?)")
        current = self.current
        if self._started and self._index < len(self._timeline) - 1:
            self._index += 1
        return current

    def up(self, *, rebuild: bool) -> None:
        self._enter("up", rebuild=rebuild)
        if not self._started:
            self._started = True
            if len(self._timeline) > 1:
                self._index = 1

    def down(self) -> None:
        self._enter("down")
        self._timeline = [self._after_down]
        self._index = 0
        self._started = False

    def logs(self, services: Sequence[str], *, tail: int, follow: bool) -> Iterator[str]:
        self._enter("logs", services=tuple(services), tail=tail, follow=follow)
        if isinstance(self._logs, Mapping):
            lines = [line for s in services for line in self._logs.get(s, ())]
        else:
            lines = list(self._logs)
        failure = self._failures.get("logs")
        return self._iter_logs(lines[-tail:] if tail > 0 else lines, failure)

    @staticmethod
    def _iter_logs(lines: list[str], failure: tuple[BaseException | type[BaseException], int | None] | None
                   ) -> Iterator[str]:
        limit = failure[1] if failure is not None else None
        for index, line in enumerate(lines):
            if limit is not None and index >= limit:
                break
            yield line
        if failure is not None:
            raise failure[0]

    def compose_version(self) -> tuple[int, int, int] | None:
        self._enter("compose_version")
        return self._compose_version

    def engine_image_id(self) -> str | None:
        self._enter("engine_image_id")
        return self._engine_image_id

    def docker_available(self) -> bool:
        self._enter("docker_available")
        return self._docker_available


class FakeSleep:
    """O `sleep` injetável: avança o `FakeClock` e registra cada espera (`waits`)."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(seconds)

    @property
    def count(self) -> int:
        return len(self.waits)

    @property
    def total(self) -> float:
        return sum(self.waits)

    def monotonic(self) -> float:
        """O relógio em segundos sobre o mesmo `FakeClock` (o `clock` injetável do `up`)."""
        return self.clock.now().timestamp()
