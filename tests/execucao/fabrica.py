"""Construtores de DTOs e projetos para os testes do Motor de Execução."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from ais4.comum.dto import MarcoDTO, TarefaDTO
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples

ETAPAS_SEMENTE = list(ProjetoAcademicoSimples.etapas)
AGORA = datetime(2026, 1, 1, tzinfo=UTC)


def tarefa(etapa: str, tentativa: int, status: str, projeto_id: UUID | None = None) -> TarefaDTO:
    return TarefaDTO(
        id=uuid4(),
        projeto_id=projeto_id or uuid4(),
        etapa_codigo=etapa,
        tentativa=tentativa,
        status=status,
        papel="p",
        modelo="m",
        entradas={},
        saidas=None,
        feedback=None,
        erro=None,
        ambiente=None,
        semente=1,
        lease_ate=None,
        iniciado_em=None,
        finalizado_em=None,
        criado_em=AGORA,
    )


def marco(codigo: str, etapa: str, status: str, ordem: int = 1) -> MarcoDTO:
    return MarcoDTO(
        id=uuid4(),
        projeto_id=uuid4(),
        codigo=codigo,
        ordem=ordem,
        nome=codigo,
        etapa_codigo=etapa,
        criterio_saida="c",
        exige_gate=True,
        papel_revisor="r",
        revisor=None,
        status=status,
        data_planejada=None,
        data_real=None,
        decisao_gate=None,
        decidido_em=None,
    )
