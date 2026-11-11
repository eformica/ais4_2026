"""Estado das etapas de um projeto a partir das tarefas e dos marcos; etapas prontas para despachar."""

from typing import Literal

from ais4.comum.dag import validar_dag
from ais4.comum.dto import MarcoDTO, TarefaDTO
from ais4.playbooks.base import Etapa

__all__ = ["EstadoEtapa", "estado_por_etapa", "etapas_prontas", "validar_dag"]

EstadoEtapa = Literal["pendente", "em_andamento", "aguardando_gate", "concluida", "falhou"]

_POR_STATUS_DA_TAREFA: dict[str, EstadoEtapa] = {
    "pronta": "em_andamento",
    "em_execucao": "em_andamento",
    "validando": "em_andamento",
    "aguardando_gate": "aguardando_gate",
    "falhou": "falhou",
    "cancelada": "pendente",
}


def estado_por_etapa(
    etapas: list[Etapa], tarefas: list[TarefaDTO], marcos: list[MarcoDTO]
) -> dict[str, EstadoEtapa]:
    """Estado de cada etapa; a última tentativa (maior `tentativa`) manda.

    Etapa sem tarefa é `pendente`. Tarefa `concluida` só fecha a etapa se ela não tem gate, ou se
    o marco dela (por `etapa_codigo`) está `aprovado`; com o marco `aguardando_gate` a etapa
    aguarda o gate, e em qualquer outro caso (`reprovado`, `em_andamento`, ainda `pendente`, sem
    marco) segue `em_andamento`: não se despacha de novo e as dependentes continuam bloqueadas.
    """
    ultima: dict[str, TarefaDTO] = {}
    for tarefa in tarefas:
        atual = ultima.get(tarefa.etapa_codigo)
        if atual is None or tarefa.tentativa > atual.tentativa:
            ultima[tarefa.etapa_codigo] = tarefa

    marco_da_etapa: dict[str, MarcoDTO] = {}
    for marco in sorted(marcos, key=lambda m: m.ordem):
        if marco.etapa_codigo is not None:
            marco_da_etapa[marco.etapa_codigo] = marco

    estado: dict[str, EstadoEtapa] = {}
    for etapa in etapas:
        tarefa = ultima.get(etapa.codigo)
        if tarefa is None:
            estado[etapa.codigo] = "pendente"
        elif tarefa.status != "concluida":
            estado[etapa.codigo] = _POR_STATUS_DA_TAREFA[tarefa.status]
        elif not etapa.exige_gate:
            estado[etapa.codigo] = "concluida"
        else:
            marco = marco_da_etapa.get(etapa.codigo)
            situacao = marco.status if marco is not None else None
            if situacao == "aprovado":
                estado[etapa.codigo] = "concluida"
            elif situacao == "aguardando_gate":
                estado[etapa.codigo] = "aguardando_gate"
            else:
                estado[etapa.codigo] = "em_andamento"
    return estado


def etapas_prontas(etapas: list[Etapa], estado: dict[str, EstadoEtapa]) -> list[str]:
    """Códigos das etapas `pendente` cujas dependências estão todas `concluida`, por `ordem`."""
    return [
        e.codigo
        for e in sorted(etapas, key=lambda e: e.ordem)
        if estado.get(e.codigo) == "pendente"
        and all(estado.get(dep) == "concluida" for dep in e.depende_de)
    ]
