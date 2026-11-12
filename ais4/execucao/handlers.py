"""Handlers da execução para eventos de projetos (A.4). Todos idempotentes.

- `ProjetoStatusAlterado` para `pausado`/`cancelado`: toda tentativa aberta (inclusive a que
  um worker está rodando) vira `cancelada`; o worker para na próxima mensagem do modelo. A
  retomada recria a tentativa, com o feedback pendente.
- `MarcoStatusAlterado` para `reprovado`: reabre a etapa com o feedback do gate.
"""

import logging
from uuid import UUID

from ais4.comum import eventos
from ais4.comum.eventos import Evento
from ais4.execucao import orquestrador, servico

log = logging.getLogger(__name__)

PARA_CANCELAR = ("pausado", "cancelado")


def ao_projeto_parado(evento: Evento) -> None:
    canceladas = servico.cancelar_abertas(UUID(str(evento.agregado_id)))
    if canceladas:
        log.info(
            "Projeto %s %s: %d tentativa(s) aberta(s) cancelada(s)",
            evento.agregado_id,
            evento.payload.get("para"),
            len(canceladas),
        )


def ao_marco_reprovado(evento: Evento) -> None:
    orquestrador.ao_reprovar_gate(UUID(str(evento.agregado_id)), evento.payload["codigo"])


def registrar() -> None:
    eventos.assinar(
        "projetos",
        "ProjetoStatusAlterado",
        ao_projeto_parado,
        filtro=lambda e: e.payload.get("para") in PARA_CANCELAR,
    )
    eventos.assinar(
        "projetos",
        "MarcoStatusAlterado",
        ao_marco_reprovado,
        filtro=lambda e: e.payload.get("para") == "reprovado",
    )
