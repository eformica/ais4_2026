from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ais4.auth import Usuario, exigir_papel, usuario_atual
from ais4.comum.dto import PropostaDTO
from ais4.criacao import motor
from ais4.criacao.motor import DecisaoProposta
from ais4.llm.cliente import ClienteAnthropic, ClienteLLM

PAPEL_RESPONSAVEL_TECNICO = "responsavel_tecnico"

router = APIRouter(prefix="/criacao", tags=["criacao"], dependencies=[Depends(usuario_atual)])

UsuarioAtual = Annotated[Usuario, Depends(usuario_atual)]
ResponsavelTecnico = Annotated[Usuario, Depends(exigir_papel(PAPEL_RESPONSAVEL_TECNICO))]

StatusProposta = Literal[
    "rascunho",
    "em_autocritica",
    "aguardando_aprovacao",
    "ajustes_solicitados",
    "aprovada",
    "rejeitada",
    "devolvida",
]


def obter_cliente_llm() -> ClienteLLM:
    """Dependência do endpoint que dispara um ciclo; os testes a substituem por `ClienteLLMFalso`."""
    return ClienteAnthropic()


ClienteAtual = Annotated[ClienteLLM, Depends(obter_cliente_llm)]


@router.get("/propostas")
def listar(
    status: StatusProposta | None = None, limite: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[PropostaDTO]:
    return motor.listar_propostas(status, limite)


@router.get("/propostas/{id}")
def obter(id: UUID) -> dict:
    """A proposta com a trilha da autocrítica e as fontes verificadas, para o revisor decidir."""
    return {
        "proposta": motor.obter_proposta(id),
        "iteracoes": motor.iteracoes_da_proposta(id),
        "fontes": motor.fontes_da_proposta(id),
    }


@router.post("/propostas/{id}/decisao")
def decidir(id: UUID, decisao: DecisaoProposta, usuario: UsuarioAtual) -> PropostaDTO:
    return motor.decidir(id, decisao, usuario)


@router.post("/ciclos")
def disparar_ciclo(usuario: ResponsavelTecnico, cliente: ClienteAtual) -> dict:
    """Roda um ciclo do motor agora (útil no piloto). Quem dispara não pode aprovar a proposta."""
    return {"proposta_id": motor.executar_ciclo(cliente, usuario.nome)}
