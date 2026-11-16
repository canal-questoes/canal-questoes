"""Fronteiras do núcleo nos relatórios e no acompanhamento (princípio
"Portas e contratos"; núcleo sem HTTP e sem adaptadores;
a porta de renderização do relatório e os adaptadores dela; o formatador injetado no cálculo do
relatório; o canal passado aos tratadores).

Arquivo à parte: `tests/contract/test_boundaries.py` e `tests/contract/test_boundaries_dispatch.py` ficam congelados e
sem edição, e os verificadores deles são reusados aqui. Cobre:
- os 6 módulos do núcleo dos relatórios (`reports`, `reportrender`, `teamnotices`, `noticetexts`, `previews`,
  `digests`) e a porta `ports/report.py` não importam HTTP, `subprocess`, `socket`, a API nem os
  adaptadores, pela AST (direto) e num processo novo (transitivo); não importam `cq_server.telegramrender`
  nem citam ou testam o canal concreto; nenhum cita `identities` em SQL (as identidades vêm de
  `events.load_write_identities`);
- a porta `ReportRenderer` (`Protocol`) em `ports/report.py` e os 3 adaptadores de `reportrender`
  (`JsonReportRenderer`, `CsvReportRenderer`, `ChannelSummaryRenderer`) com o método `render(report)` da
  porta;
- a raiz de composição em execução: `run_worker(…, console_routes=True)` registra os 11 tratadores dos relatórios
  (`report.freeze`, os 8 de `teamnotices`, `preview.send`, `digest.daily`) só pelas portas (com um motor e
  uma obtenção que só cumprem as portas, ou sem eles, e com um canal que só cumpre a porta e lança na leitura
  de `formatter`/`formatter_for`: nenhum deles o lê na montagem), passa
  `notice_content=NoticeContentLoader()` ao `Outbox` e os tipos dos relatórios ao `Sweeper`; sem `bots` e sem
  `console_routes`, nada dos relatórios; `scheduler.REPORT_RULES` com os 11 tipos na ordem de `REPORT_TASK_TYPES` e
  `DISPATCH_RULES` com os mesmos 8 do disparo e da resposta;
- a **ligação**: o corpo de `reports.get` em JSON e em CSV é o `body` de
  `JsonReportRenderer().render(report)` e de `CsvReportRenderer().render(report)` (dublês dos renderizadores
  injetados por `monkeypatch` acusam a chamada), e o resumo do aviso `dispatch_closed` sai de
  `ChannelSummaryRenderer(channel.formatter, …).render(report_from_dict(content))`, chamado pelo
  `teamnotices.deliver` na entrega do congelamento.

Falha esperada no esqueleto (nenhuma função passa no esqueleto): o `run_worker` ainda não
registra os tratadores dos relatórios (contrato não cumprido até a implementação), `REPORT_RULES` é
vazio, e o congelamento lança `NotImplementedError` (a semente de relatórios para em `report.freeze`),
com as rotas do relatório ausentes. As checagens estruturais, que já valem no esqueleto,
ficam na mesma função da raiz de composição, no molde de
`test_dispatch_core_modules_import_nothing_forbidden_and_get_engine_and_fetcher_only_by_ports`.
"""

from __future__ import annotations

import dataclasses
import inspect
import io
from datetime import timedelta
from typing import Any

import pytest

from tests.contract.test_boundaries import TRANSITIVE_FORBIDDEN, _identities_sql, _is_forbidden, _module_files
from tests.contract.test_boundaries_dispatch import (
    _PortChannel,
    _PortEngine,
    _PortFetcher,
    _channel_offenses,
    _forbid_adapter_construction,
    _imports_telegramrender,
    _loaded_by,
    _dispatch_violations,
    _registered_handlers,
)

# Os módulos do núcleo dos relatórios (a porta `ports/report.py` entra por `ports`).
REPORT_CORE_MODULES = ("reports", "reportrender", "teamnotices", "noticetexts", "previews", "digests")
REPORT_PORT_MODULE = "cq_server.ports.report"
# Os 11 tipos de tarefa, na ordem de `scheduler.REPORT_RULES`.
REPORT_TASK_TYPES = ("report.freeze", "notice.send", "notice.dispatch_rejected", "notice.dispatch_cancelled",
                 "notice.dispatch_opened", "notice.question_failed", "notice.access_episode",
                 "notice.invite_batch", "notice.invite_window", "preview.send", "digest.daily")
# Os 8 tipos de `DISPATCH_RULES`, na ordem dela (os relatórios não mudam `DISPATCH_RULES`).
DISPATCH_RULE_TYPES = ("dispatch.validate", "dispatch.expire", "dispatch.open", "dispatch.close", "delivery.prepare",
                 "submission.grade", "delivery.reveal", "telegram.update")
FRONTIER_DB_URL = "postgresql://cq-fronteiras.invalid/cq"


def _report_task_types() -> tuple[str, ...]:
    """Os 11 tipos de tarefa dos relatórios pelas constantes dos módulos."""
    from cq_server import digests, previews, reports, teamnotices

    return (reports.FREEZE_TASK_TYPE, teamnotices.SEND_TASK_TYPE, teamnotices.DISPATCH_REJECTED_TASK_TYPE,
            teamnotices.DISPATCH_CANCELLED_TASK_TYPE, teamnotices.DISPATCH_OPENED_TASK_TYPE,
            teamnotices.QUESTION_FAILED_TASK_TYPE, teamnotices.ACCESS_EPISODE_TASK_TYPE,
            teamnotices.INVITE_BATCH_TASK_TYPE, teamnotices.INVITE_WINDOW_TASK_TYPE, previews.PREVIEW_TASK_TYPE,
            digests.DIGEST_TASK_TYPE)


def _report_sources() -> dict[str, str]:
    """Nome qualificado → código de cada módulo dos relatórios e da porta `ports/report.py`."""
    sources: dict[str, str] = {}
    for name in REPORT_CORE_MODULES:
        for modname, path, _ in _module_files(name):
            sources[modname] = path.read_text(encoding="utf-8")
    for modname, path, _ in _module_files("ports"):
        if modname == REPORT_PORT_MODULE:
            sources[modname] = path.read_text(encoding="utf-8")
    return sources


def _record_outbox_and_sweeper(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], list[frozenset[str]]]:
    """Troca o `Outbox` e o `Sweeper` que o worker monta por fábricas que registram os argumentos nomeados
    (do `Outbox`) e os `handled_types` (do `Sweeper`) e montam os objetos reais."""
    from cq_server import worker

    outboxes: list[dict[str, Any]] = []
    sweepers: list[frozenset[str]] = []
    real_outbox, real_sweeper = worker.Outbox, worker.Sweeper

    def outbox(*args: Any, **kwargs: Any) -> Any:
        outboxes.append(dict(kwargs))
        return real_outbox(*args, **kwargs)

    def sweeper(*args: Any, **kwargs: Any) -> Any:
        sweepers.append(frozenset(kwargs.get("handled_types", ())))
        return real_sweeper(*args, **kwargs)

    monkeypatch.setattr(worker, "Outbox", outbox)
    monkeypatch.setattr(worker, "Sweeper", sweeper)
    return outboxes, sweepers


def _render_signature(cls: type) -> tuple[list[str], Any, Any]:
    signature = inspect.signature(cls.render)
    params = list(signature.parameters.values())
    return [p.name for p in params], params[-1].annotation if params else None, signature.return_annotation


# ---------------------------------------------------------------------------------------------
# Núcleo sem HTTP, processos, rede nem adaptadores; a porta do relatório; a raiz de composição
# ---------------------------------------------------------------------------------------------


def test_report_modules_respect_the_frontier_and_the_worker_registers_the_report_handlers_by_ports(
        monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    """Portas e contratos; o canal passado aos tratadores; a porta de renderização do relatório; estrutura,
    worker, API e cliente, e varredura.

    Estrutura: cada módulo do núcleo dos relatórios e a porta `ports/report.py` não importam HTTP,
    `subprocess`, `socket`, a API nem os adaptadores (AST, com as formas de importação do congelado) nem os
    carregam num processo novo; não importam `cq_server.telegramrender`, não citam `TelegramChannel` nem
    `ConsoleChannel` e não testam o tipo do canal; nenhum cita `identities` em SQL; controles positivos
    sintéticos de cada checagem. A porta `ReportRenderer` é um `Protocol` com o único membro
    `render(report) -> RenderedReport`, e os 3 adaptadores de `reportrender` o têm com a mesma assinatura e
    montam só com os argumentos (`ChannelSummaryRenderer(formatter, *, timezone, offering_code,
    resend=False)`).

    Em execução: sem `bots` e sem `console_routes`, o `run_worker` fica sem os relatórios (nenhum tratador deles,
    `Outbox` sem `notice_content`, sem varredura); com `console_routes=True`, registra os 11 tratadores dos relatórios
    recebendo um motor e uma obtenção que só cumprem as portas, e também sem eles, sem montar adaptador, com
    um canal que só cumpre a porta e lança na leitura de `formatter`/`formatter_for` (nenhum deles
    o lê na montagem); passa `notice_content=NoticeContentLoader()` ao `Outbox` e os 11 tipos aos
    `handled_types` do `Sweeper`. `scheduler.REPORT_RULES` tem os 11 tipos na ordem de `REPORT_TASK_TYPES`, e
    `DISPATCH_RULES` continua com os mesmos 8 tipos do disparo e da resposta, na ordem."""
    # Estrutura: importações diretas (AST) e transitivas.
    problems: dict[str, list[str]] = {}
    for module in (*REPORT_CORE_MODULES, "ports"):
        files = _module_files(module)
        assert files, f"módulo do núcleo cq_server.{module} não encontrado (a estrutura do repositório)"
        if module == "ports":
            assert REPORT_PORT_MODULE in {m for m, _, _ in files}
        found = [f"{modname}:{line} importa {name}" for modname, path, is_pkg in files
                 for line, name in _dispatch_violations(path.read_text(encoding="utf-8"), modname, is_pkg)]
        if found:
            problems[module] = found
    assert problems == {}
    sources = _report_sources()
    assert set(sources) == {*(f"cq_server.{m}" for m in REPORT_CORE_MODULES), REPORT_PORT_MODULE}
    loaded = _loaded_by(sorted(sources))
    assert set(sources) <= set(loaded)
    assert [m for m in loaded if _is_forbidden(m, TRANSITIVE_FORBIDDEN)] == []

    # Estrutura: sem `telegramrender`, sem canal concreto, sem `identities` em SQL.
    assert {m: hits for m, src in sources.items() if (hits := _imports_telegramrender(src, m))} == {}
    assert {m: hits for m, src in sources.items() if (hits := _channel_offenses(src))} == {}
    assert {m: hits for m, src in sources.items() if (hits := _identities_sql(src))} == {}
    for source, modname, expected in (
        ("from cq_server.adapters.console import ConsoleChannel", "cq_server.teamnotices", "cq_server.adapters.console"),
        ("import socket", "cq_server.digests", "socket"),
        ("from subprocess import run", "cq_server.reportrender", "subprocess"),
        ("def f():\n    import httpx2\n    return httpx2", "cq_server.previews", "httpx2"),
        ("from ..api import routes", "cq_server.ports.report", "cq_server.api"),
    ):
        assert expected in {name for _, name in _dispatch_violations(source, modname)}, source
    assert _imports_telegramrender("from .telegramrender import PlainFormatter", "cq_server.reportrender") != []
    assert _channel_offenses("def deliver(channel):\n    return isinstance(channel, object)") != []
    assert _identities_sql('conn.execute("SELECT ra FROM identities WHERE person_id = %s")') == ["FROM identities"]

    # Estrutura: a porta `ReportRenderer` e os 3 adaptadores.
    from cq_server import reportrender
    from cq_server.ports import report as port
    from cq_server.telegramrender import PlainFormatter

    assert getattr(port.ReportRenderer, "_is_protocol", False) is True
    assert {name for name, value in vars(port.ReportRenderer).items()
            if callable(value) and not name.startswith("_")} == {"render"}
    assert [f.name for f in dataclasses.fields(port.RenderedReport)] == ["media_type", "body"]
    expected_signature = _render_signature(port.ReportRenderer)
    assert expected_signature == (["self", "report"], "Report", "RenderedReport")
    adapters = (reportrender.JsonReportRenderer(), reportrender.CsvReportRenderer(),
                reportrender.ChannelSummaryRenderer(PlainFormatter(), timezone="America/Sao_Paulo",
                                                    offering_code="mc102-2031s1"))
    for adapter in adapters:
        assert _render_signature(type(adapter)) == expected_signature, type(adapter).__name__
    summary_params = inspect.signature(reportrender.ChannelSummaryRenderer).parameters
    assert [(p.name, p.kind.name) for p in summary_params.values()] == [
        ("formatter", "POSITIONAL_OR_KEYWORD"), ("timezone", "KEYWORD_ONLY"), ("offering_code", "KEYWORD_ONLY"),
        ("resend", "KEYWORD_ONLY")]
    assert summary_params["resend"].default is False

    # Em execução: a raiz de composição.
    from cq_server import scheduler, students
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.previews import NoticeContentLoader

    r6_types = _report_task_types()
    assert r6_types == REPORT_TASK_TYPES
    built = _forbid_adapter_construction(monkeypatch)
    outboxes, sweepers = _record_outbox_and_sweeper(monkeypatch)

    r4_only = _registered_handlers(monkeypatch, settings, channel=ConsoleChannel(io.StringIO()),
                                   engine=_PortEngine(), fetcher=_PortFetcher())
    assert set(r4_only) == {students.INVITE_EMAIL_TASK_TYPE}
    assert [kwargs.get("notice_content") for kwargs in outboxes] == [None]
    assert sweepers == []
    outboxes.clear()

    channel = _PortChannel()
    with_ports = _registered_handlers(monkeypatch, settings, channel=channel, engine=_PortEngine(),
                                      fetcher=_PortFetcher(), console_routes=True)
    assert sorted(set(r6_types) - set(with_ports)) == [], "tratadores novos ausentes do run_worker"
    assert channel.reads == [], "um tratador leu o formatter do canal na montagem"
    assert len(outboxes) == 1 and isinstance(outboxes[0].get("notice_content"), NoticeContentLoader), outboxes
    assert len(sweepers) == 1 and set(r6_types) <= sweepers[0], sweepers
    outboxes.clear()
    sweepers.clear()

    without = _registered_handlers(monkeypatch, settings, channel=_PortChannel(), console_routes=True)
    assert sorted(set(r6_types) - set(without)) == [], "tratadores novos ausentes do run_worker (sem motor)"
    assert len(outboxes) == 1 and isinstance(outboxes[0].get("notice_content"), NoticeContentLoader), outboxes
    assert len(sweepers) == 1 and set(r6_types) <= sweepers[0], sweepers
    assert built == []

    # A varredura: as 11 regras dos relatórios na ordem de `REPORT_TASK_TYPES`; `DISPATCH_RULES` sem mudança.
    assert tuple(rule.task_type for rule in scheduler.REPORT_RULES) == REPORT_TASK_TYPES
    assert tuple(rule.task_type for rule in scheduler.DISPATCH_RULES) == DISPATCH_RULE_TYPES


# ---------------------------------------------------------------------------------------------
# A ligação: a rota e o aviso de fechamento compõem o relatório pela porta
# ---------------------------------------------------------------------------------------------


def test_report_route_and_closing_summary_go_through_the_report_port(
        monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, client: Any, clock: Any, sql: Any,
        team_members: Any) -> None:
    """Portas e contratos; o formato do relatório; a porta de renderização do relatório.

    Um disparo do oferecimento da equipe simulada (2 membros com endereço ativo) é validado, confirmado,
    aberto, fechado e congelado com o relógio próprio da semente de relatórios, com dublês dos 3
    renderizadores injetados por `monkeypatch` (cada um registra a chamada e devolve o resultado do
    adaptador real):
    - `reports.get` da versão 1: o corpo JSON é exatamente o `body` de `JsonReportRenderer().render(report)`
      e o CSV, o de `CsvReportRenderer().render(report)`, com o mesmo `Report` (o do JSON publicado, relido
      por `report_from_dict`) e a mídia do `RenderedReport`;
    - o resumo do aviso `dispatch_closed`: o `teamnotices.deliver` monta `ChannelSummaryRenderer` com o
      formatador do canal (`channel.formatter`, o do `TelegramChannel`), o fuso e o código do oferecimento,
      sem `resend`, e o renderiza sobre `report_from_dict(content)` da versão congelada (`frozen` nulo); o
      texto enfileirado a cada membro ativo é exatamente esse `body`.

    Vermelho no esqueleto: o tratador `report.freeze` lança `NotImplementedError` (a semente para nele),
    e a rota do relatório não existe."""
    from cq_server import reportrender, reports
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.bots import TelegramChannel
    from cq_server.clock import SystemClock
    from cq_server.config import DeliverySettings
    from tests.helpers.api import ADMIN_TOKEN, auth
    from tests.helpers.calls import REPORT_WINDOW_MINUTES, SEED_QUESTION, _close_seeded_reports, _seed_call
    from tests.helpers.clock import iso
    from tests.helpers.contract import REPORT_CSV_MEDIA, REPORT_JSON_MEDIA
    from tests.helpers.team import team_messages

    json_calls: list[tuple[Any, Any]] = []
    csv_calls: list[tuple[Any, Any]] = []
    summary_inits: list[tuple[Any, tuple, dict[str, Any]]] = []
    summary_calls: list[tuple[Any, Any]] = []
    real_json = reportrender.JsonReportRenderer.render
    real_csv = reportrender.CsvReportRenderer.render
    real_init = reportrender.ChannelSummaryRenderer.__init__
    real_summary = reportrender.ChannelSummaryRenderer.render

    def json_double(self: Any, report: Any) -> Any:
        rendered = real_json(self, report)
        json_calls.append((report, rendered))
        return rendered

    def csv_double(self: Any, report: Any) -> Any:
        rendered = real_csv(self, report)
        csv_calls.append((report, rendered))
        return rendered

    def init_double(self: Any, formatter: Any, *args: Any, **kwargs: Any) -> None:
        summary_inits.append((formatter, args, dict(kwargs)))
        real_init(self, formatter, *args, **kwargs)

    def summary_double(self: Any, report: Any) -> Any:
        rendered = real_summary(self, report)
        summary_calls.append((report, rendered))
        return rendered

    monkeypatch.setattr(reportrender.JsonReportRenderer, "render", json_double)
    monkeypatch.setattr(reportrender.CsvReportRenderer, "render", csv_double)
    monkeypatch.setattr(reportrender.ChannelSummaryRenderer, "__init__", init_double)
    monkeypatch.setattr(reportrender.ChannelSummaryRenderer, "render", summary_double)

    code = team_members.code
    closes_at = iso(clock.now() + timedelta(minutes=REPORT_WINDOW_MINUTES))
    prepared = _seed_call(client, "POST", f"/v1/offerings/{code}/dispatches", "dispatches.prepare",
                          {**SEED_QUESTION, "closes_at": closes_at})
    dispatch_id = prepared["dispatch_id"]
    _close_seeded_reports(client, request, clock, code, [dispatch_id])

    # O resumo de fechamento, composto pela porta na entrega do congelamento.
    expected_formatter = TelegramChannel(FRONTIER_DB_URL, clock=SystemClock(), runtime=None,
                                         default=ConsoleChannel(io.StringIO()), delivery=DeliverySettings()).formatter
    assert summary_inits, "o aviso dispatch_closed não montou o ChannelSummaryRenderer"
    for formatter, args, kwargs in summary_inits:
        assert type(formatter) is type(expected_formatter)
        assert args == ()
        assert {**kwargs, "resend": kwargs.get("resend", False)} == {
            "timezone": team_members.offering["timezone"], "offering_code": code, "resend": False}
    assert summary_calls, "o aviso dispatch_closed não renderizou o resumo pela porta"

    # A rota: o corpo é o do renderizador, com o mesmo `Report`.
    path = f"/v1/offerings/{code}/dispatches/{dispatch_id}/report"
    resp = client.get(path, params={"version": "1"}, headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"reports.get: {resp.status_code} {resp.text[:300]}"
    assert len(json_calls) == 1, "a rota não passou pelo JsonReportRenderer"
    report, rendered = json_calls[0]
    assert type(report) is reports.Report
    assert report == reports.report_from_dict(resp.json())
    assert (rendered.media_type, resp.headers["content-type"]) == (REPORT_JSON_MEDIA, REPORT_JSON_MEDIA)
    assert resp.content == rendered.body.encode("utf-8")
    assert report.frozen is not None and (report.frozen.version, report.frozen.trigger) == (1, "closing")

    resp_csv = client.get(path, params={"version": "1", "format": "csv"}, headers=auth(ADMIN_TOKEN))
    assert resp_csv.status_code == 200, f"reports.get csv: {resp_csv.status_code} {resp_csv.text[:300]}"
    assert len(csv_calls) == 1, "a rota não passou pelo CsvReportRenderer"
    csv_report, csv_rendered = csv_calls[0]
    assert csv_report == report
    assert (csv_rendered.media_type, resp_csv.headers["content-type"]) == (REPORT_CSV_MEDIA, REPORT_CSV_MEDIA)
    assert resp_csv.content == csv_rendered.body.encode("utf-8")

    # O resumo foi renderizado sobre o `content` congelado (`frozen` nulo) e é o texto enfileirado à equipe.
    stored = dataclasses.replace(report, frozen=None)
    bodies = {summary.body for _, summary in summary_calls}
    assert [summary_report == stored for summary_report, _ in summary_calls] == [True] * len(summary_calls)
    assert len(bodies) == 1
    [body] = bodies
    messages = team_messages(sql, "dispatch_closed")
    assert [m.recipient for m in messages] == team_members.recipients
    assert [(m.text, m.text_format) for m in messages] == [(body, expected_formatter.text_format)] * len(messages)
