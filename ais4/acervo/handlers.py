"""Handlers do Acervo para eventos de outros módulos (A.4). Todos idempotentes.

Cada handler lê o status atual da ideia antes de agir: se já está no alvo, não faz nada; se está
no status de origem esperado, transiciona; em qualquer outro status avisa e dá o evento por
tratado (um estado inesperado não pode travar o outbox para sempre).
"""

import logging
from collections.abc import Callable
from uuid import UUID

from ais4.acervo import servico
from ais4.comum import eventos
from ais4.comum.eventos import Evento
from ais4.playbooks import servico as playbooks

log = logging.getLogger(__name__)

USUARIO = "sistema:outbox"


def _mover_ideia(evento: Evento, de: str, para: str, motivo: str) -> None:
    ideia_id = UUID(str(evento.payload["ideia_origem_id"]))
    atual = servico.obter(ideia_id).status
    if atual == para:
        return
    if atual != de:
        log.warning(
            "Evento %s (%s): a ideia %s está em %r (esperado %r ou %r); nada a fazer",
            evento.id,
            evento.tipo,
            ideia_id,
            atual,
            de,
            para,
        )
        return
    servico.transicionar(ideia_id, para, USUARIO, motivo)


def ao_projeto_criado(evento: Evento) -> None:
    _mover_ideia(evento, "qualificada", "em_projeto", f"projeto {evento.agregado_id} criado")


def ao_projeto_cancelado(evento: Evento) -> None:
    _mover_ideia(evento, "em_projeto", "qualificada", f"projeto {evento.agregado_id} cancelado")


def ao_projeto_concluido(evento: Evento) -> None:
    _mover_ideia(evento, "em_projeto", "concluida", f"projeto {evento.agregado_id} concluído")


def ao_playbook_validado(evento: Evento) -> None:
    """Lacunas abertas compatíveis com o playbook validado voltam para `reabrir` (processo 1.2)."""
    classes = playbooks.classe_de_problema(evento.agregado_id)
    reabertas = servico.marcar_lacunas_para_reabrir(classes, USUARIO)
    if reabertas:
        log.info(
            "Playbook %s v%s validado: %d lacuna(s) marcada(s) para reabrir",
            evento.agregado_id,
            evento.payload.get("versao"),
            len(reabertas),
        )


def _para(valor: str) -> Callable[[Evento], bool]:
    return lambda e: e.payload.get("para") == valor


def registrar() -> None:
    """Assina os handlers no publicador (idempotente)."""
    eventos.assinar("projetos", "ProjetoCriado", ao_projeto_criado)
    eventos.assinar("projetos", "ProjetoStatusAlterado", ao_projeto_cancelado, _para("cancelado"))
    eventos.assinar("projetos", "ProjetoStatusAlterado", ao_projeto_concluido, _para("concluido"))
    eventos.assinar(
        "playbooks", "PlaybookVersaoStatusAlterado", ao_playbook_validado, _para("validado")
    )
