from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ais4.acervo import servico, similaridade
from ais4.acervo.servico import IdeiaAtualizar, IdeiaCriar, ResultadoQualificacao, Status
from ais4.acervo.similaridade import Embedder, VoyageEmbedder
from ais4.auth import Usuario, usuario_atual
from ais4.comum.dto import EventoDTO, IdeiaDTO
from ais4.comum.erros import Proibido, RegraViolada

router = APIRouter(prefix="/acervo", tags=["acervo"], dependencies=[Depends(usuario_atual)])

UsuarioAtual = Annotated[Usuario, Depends(usuario_atual)]


def obter_embedder() -> Embedder:
    """Dependência dos endpoints que geram embeddings; os testes a substituem por `EmbedderFalso`."""
    return VoyageEmbedder()


EmbedderAtual = Annotated[Embedder, Depends(obter_embedder)]


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


def _autorizar_transicao(id: UUID, para: str, usuario: Usuario) -> None:
    """Só o curador transita, exceto rascunho -> triagem, que o autor da ideia também pode."""
    if para == "triagem":
        ideia = servico.obter(id)
        if ideia.status == "rascunho" and ideia.criado_por == usuario.nome:
            return
    servico.exigir_curador(usuario)


@router.post("/ideias/{id}/transicao")
def transicionar(id: UUID, corpo: Transicao, usuario: UsuarioAtual) -> IdeiaDTO:
    if corpo.para == "qualificada":
        raise RegraViolada("A qualificação passa pelo endpoint de qualificação, não por este")
    _autorizar_transicao(id, corpo.para, usuario)
    return servico.transicionar(id, corpo.para, usuario.nome, corpo.motivo, corpo.fundida_em_id)


@router.get("/ideias/{id}/historico")
def historico(id: UUID) -> list[EventoDTO]:
    return servico.historico(id)


class Similar(BaseModel):
    ideia: IdeiaDTO
    similaridade: float


@router.post("/ideias/{id}/qualificacao")
def qualificar(id: UUID, usuario: UsuarioAtual, embedder: EmbedderAtual) -> ResultadoQualificacao:
    return servico.qualificar(id, usuario, embedder)


@router.get("/ideias/{id}/similares")
def similares(id: UUID, limite: Annotated[int, Query(ge=1, le=50)] = 5) -> list[Similar]:
    """Somente leitura: a ideia precisa já estar indexada (409 se não estiver)."""
    return [Similar(ideia=i, similaridade=s) for i, s in similaridade.similares(id, limite)]


class RevisaoDuplicidade(BaseModel):
    candidata_id: UUID
    decisao: Literal["distinta", "duplicada"]


@router.post("/ideias/{id}/duplicidade")
def revisar_duplicidade(
    id: UUID, corpo: RevisaoDuplicidade, usuario: UsuarioAtual, embedder: EmbedderAtual
) -> dict:
    servico.exigir_curador(usuario)
    if servico.obter(id).criado_por == usuario.nome:
        raise Proibido("O autor da ideia não pode revisar a duplicidade dela")
    servico.obter(corpo.candidata_id)
    similaridade.indexar(id, embedder)
    similaridade.indexar(corpo.candidata_id, embedder)
    sim = similaridade.similaridade_entre(id, corpo.candidata_id)
    servico.registrar_revisao_duplicidade(id, corpo.candidata_id, sim, corpo.decisao, usuario.nome)
    return {
        "ideia_id": id,
        "candidata_id": corpo.candidata_id,
        "similaridade": sim,
        "decisao": corpo.decisao,
        "revisor": usuario.nome,
    }


class NovaLacuna(BaseModel):
    descricao: str
    classes: list[str] = []


@router.post("/ideias/{id}/lacunas", status_code=201)
def registrar_lacuna(id: UUID, corpo: NovaLacuna, usuario: UsuarioAtual) -> dict:
    return {"id": servico.registrar_lacuna(id, corpo.descricao, corpo.classes, usuario)}
