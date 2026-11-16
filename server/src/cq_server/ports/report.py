"""Porta do relatório: o renderizador que transforma o relatório de um disparo no corpo de uma resposta
(o núcleo não depende do formato de saída).

O núcleo produz o relatório como um `reports.Report` (o objeto da resposta de `reports.get`) e o entrega a um
renderizador desta porta, que devolve o corpo pronto e o tipo de mídia. Os adaptadores puros ficam no
núcleo, em `cq_server.reportrender` (`JsonReportRenderer`, `CsvReportRenderer`,
`ChannelSummaryRenderer`); um relatório formatado seria um quarto adaptador (ainda não existe).

Só o tipo do resultado e o `Protocol`; nada de HTTP (já coberto por `CORE_MODULES`, entrada `ports`).
O tipo `Report` só é importado para a anotação (`TYPE_CHECKING`): a porta não carrega o domínio.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cq_server.reports import Report


@dataclass(frozen=True)
class RenderedReport:
    """O relatório renderizado: o tipo de mídia (ex.: `application/json`, `text/csv; charset=utf-8`)
    e o corpo, como texto (o CSV já com o BOM; o resumo no markup do dialeto do formatador)."""

    media_type: str
    body: str


class ReportRenderer(Protocol):
    """Renderiza um relatório: o único caminho de composição do relatório."""

    def render(self, report: Report) -> RenderedReport: ...
