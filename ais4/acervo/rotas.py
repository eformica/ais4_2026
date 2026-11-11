from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ais4.acervo import servico
from ais4.acervo.servico import IdeiaAtualizar, IdeiaCriar, Status
from ais4.auth import Usuario, usuario_atual
from ais4.comum.dto import EventoDTO, IdeiaDTO
from ais4.comum.erros import Proibido, RegraViolada

PAPEL_CURADOR = "curador_do_acervo"

router = APIRouter(prefix="/acervo", tags=["acervo"], dependencies=[Depends(usuario_atual)])

UsuarioAtual = Annotated[Usuario, Depends(usuario_atual)]


class Transicao(BaseModel):
    para: Status
    motivo: str | None = None
    fundida_em_id: UUID | None = None


@router.post("/ideias", status_code=201)
def criar(dados: IdeiaCriar, usuario: UsuarioAtual) -> IdeiaDTO:
    return servico.criar_ideia(dados, usuario.nome)


@router.get("/ideias")
def listar(
    status: Status = "qualificada", limite: Annotated[int, Query(ge=1, le=200)] = 20
) -> list[IdeiaDTO]:
    return servico.fila(status, limite)


@router.get("/ideias/{id}")
def obter(id: UUID) -> IdeiaDTO:
    return servico.obter(id)


@router.patch("/ideias/{id}")
def atualizar(id: UUID, dados: IdeiaAtualizar, usuario: UsuarioAtual) -> IdeiaDTO:
    return servico.atualizar(id, dados, usuario.nome)


def _exigir_curador(usuario: Usuario) -> None:
    if PAPEL_CURADOR not in usuario.papeis:
        raise Proibido(f"Requer o papel {PAPEL_CURADOR!r}")


def _autorizar_transicao(id: UUID, para: str, usuario: Usuario) -> None:
    """Só o curador transita, exceto rascunho -> triagem, que o autor da ideia também pode."""
    if para == "triagem":
        ideia = servico.obter(id)
        if ideia.status == "rascunho" and ideia.criado_por == usuario.nome:
            return
    _exigir_curador(usuario)


@router.post("/ideias/{id}/transicao")
def transicionar(id: UUID, corpo: Transicao, usuario: UsuarioAtual) -> IdeiaDTO:
    if corpo.para == "qualificada":
        raise RegraViolada("A qualificação passa pelo endpoint de qualificação, não por este")
    _autorizar_transicao(id, corpo.para, usuario)
    return servico.transicionar(id, corpo.para, usuario.nome, corpo.motivo, corpo.fundida_em_id)


@router.get("/ideias/{id}/historico")
def historico(id: UUID) -> list[EventoDTO]:
    return servico.historico(id)
