from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ais4.auth import Usuario, exigir_papel, usuario_atual
from ais4.comum.dto import (
    ArtefatoDTO,
    AvaliacaoDTO,
    EspecificacaoDTO,
    MarcoDTO,
    ProjetoDTO,
    ProximoPassoDTO,
)
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.comum.espec import EspecProjeto, Nota
from ais4.execucao import orcamento
from ais4.execucao.artefatos import Armazem
from ais4.projetos import servico

PAPEL_DONO = "dono_do_projeto"
PAPEL_CURADOR = "curador_do_acervo"

router = APIRouter(prefix="/projetos", tags=["projetos"], dependencies=[Depends(usuario_atual)])

UsuarioAtual = Annotated[Usuario, Depends(usuario_atual)]
Curador = Annotated[Usuario, Depends(exigir_papel(PAPEL_CURADOR))]

StatusProjeto = Literal[
    "aprovado", "em_execucao", "em_revisao", "concluido", "pausado", "cancelado"
]


def _exigir_dono(projeto_id: UUID, usuario: Usuario) -> ProjetoDTO:
    """Só o dono do projeto (a pessoa, com o papel) altera especificação e estado."""
    projeto = servico.obter(projeto_id)
    if PAPEL_DONO not in usuario.papeis or usuario.nome != projeto.dono:
        raise Proibido(f"Só o dono do projeto ({projeto.dono}) pode fazer isto")
    return projeto


# --- leitura (qualquer usuário autenticado) -----------------------------------------------
@router.get("")
def listar(
    status: StatusProjeto | None = None, limite: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[ProjetoDTO]:
    return servico.listar(status, limite)


@router.get("/{id}")
def obter(id: UUID) -> ProjetoDTO:
    return servico.obter(id)


@router.get("/{id}/especificacao")
def especificacao(id: UUID, versao: Annotated[int | None, Query(ge=1)] = None) -> EspecificacaoDTO:
    return servico.obter_especificacao(id, versao)


@router.get("/{id}/marcos")
def marcos(id: UUID) -> list[MarcoDTO]:
    return servico.marcos(id)


@router.get("/{id}/artefatos")
def artefatos(id: UUID) -> list[ArtefatoDTO]:
    """Metadados dos artefatos; o conteúdo está em `/{id}/artefatos/{aid}/conteudo`."""
    return servico.artefatos(id)


def obter_armazem() -> Armazem:
    """Dependência: o armazém configurado (`AIS4_ARTEFATOS_DIR`); testes a substituem."""
    return Armazem()


def _blocos(arquivo, tamanho: int = 1024 * 1024):
    with arquivo:
        while bloco := arquivo.read(tamanho):
            yield bloco


@router.get("/{id}/artefatos/{aid}/conteudo")
def conteudo_do_artefato(
    id: UUID, aid: UUID, armazem: Annotated[Armazem, Depends(obter_armazem)]
) -> StreamingResponse:
    """Stream do arquivo do artefato, após conferir o SHA-256 registrado."""
    artefato = next((a for a in servico.artefatos(id) if a.id == aid), None)
    if artefato is None:
        raise NaoEncontrado(f"Artefato {aid} não encontrado no projeto {id}")
    try:
        integro = armazem.verificar(artefato.uri, artefato.hash_sha256)
        arquivo = armazem.abrir(artefato.uri) if integro else None
    except (ValueError, OSError) as erro:
        raise NaoEncontrado(f"Conteúdo do artefato {aid} indisponível no armazém") from erro
    if arquivo is None:
        raise RegraViolada(f"artefato adulterado: o conteúdo não confere com o hash de {aid}")
    nome = quote(artefato.nome, safe="")
    return StreamingResponse(
        _blocos(arquivo),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{nome}"},
    )


# --- especificação e estado do projeto (dono) ---------------------------------------------
class NovaEspecificacao(BaseModel):
    especificacao: EspecProjeto
    justificativa: str = ""  # vazia vira 409 no serviço, com mensagem clara


@router.post("/{id}/especificacao", status_code=201)
def nova_especificacao(id: UUID, corpo: NovaEspecificacao, usuario: UsuarioAtual) -> dict:
    _exigir_dono(id, usuario)
    versao = servico.nova_versao_especificacao(
        id, corpo.especificacao, corpo.justificativa, usuario.nome
    )
    return {"versao": versao}


class Transicao(BaseModel):
    para: StatusProjeto
    motivo: str | None = None


@router.post("/{id}/transicao")
def transicionar(id: UUID, corpo: Transicao, usuario: UsuarioAtual) -> ProjetoDTO:
    _exigir_dono(id, usuario)
    return servico.transicionar(id, corpo.para, usuario.nome, corpo.motivo)


class OrcamentoDaExecucao(BaseModel):
    """Corpo opcional de início/retomada; campos omitidos mantêm o orçamento atual (ou o padrão)."""

    max_tokens: int | None = Field(None, gt=0)
    max_custo_usd: Decimal | None = Field(None, gt=0)
    prazo: datetime | None = None


def _entrar_em_execucao(
    id: UUID, usuario: Usuario, corpo: OrcamentoDaExecucao | None, de: str
) -> ProjetoDTO:
    projeto = _exigir_dono(id, usuario)
    if projeto.status != de:
        raise RegraViolada(f"O projeto está {projeto.status!r}; esta ação exige {de!r}")
    if corpo is not None and corpo.model_fields_set:
        atual = orcamento.obter_ou_padrao(id, usuario.nome)
        orcamento.definir(
            id,
            corpo.max_tokens or atual.max_tokens,
            corpo.max_custo_usd or atual.max_custo_usd,
            corpo.prazo if "prazo" in corpo.model_fields_set else atual.prazo,
            usuario.nome,
        )
    return servico.transicionar(id, "em_execucao", usuario.nome)


@router.post("/{id}/execucao")
def iniciar_execucao(
    id: UUID, usuario: UsuarioAtual, corpo: OrcamentoDaExecucao | None = None
) -> ProjetoDTO:
    """`aprovado -> em_execucao` (dono), definindo antes o orçamento informado (R7)."""
    return _entrar_em_execucao(id, usuario, corpo, "aprovado")


@router.post("/{id}/retomada")
def retomar_execucao(
    id: UUID, usuario: UsuarioAtual, corpo: OrcamentoDaExecucao | None = None
) -> ProjetoDTO:
    """`pausado -> em_execucao` (dono), com ajuste opcional do orçamento; abre nova rodada de
    tentativas para as etapas que falharam (R24)."""
    return _entrar_em_execucao(id, usuario, corpo, "pausado")


@router.post("/{id}/conclusao")
def concluir(id: UUID, usuario: UsuarioAtual) -> ProjetoDTO:
    """Atalho para `em_revisao -> concluido`; o banco confere lições, próximos passos e gates."""
    _exigir_dono(id, usuario)
    return servico.transicionar(id, "concluido", usuario.nome)


# --- gates, avaliação e próximos passos ---------------------------------------------------
class DecisaoGate(BaseModel):
    decisao: Literal["aprovado", "reprovado"]
    comentario: str = ""


@router.post("/{id}/marcos/{codigo}/decisao")
def decidir_gate(id: UUID, codigo: str, corpo: DecisaoGate, usuario: UsuarioAtual) -> MarcoDTO:
    return servico.decidir_gate(id, codigo, corpo.decisao, corpo.comentario, usuario)


class NovaAvaliacao(BaseModel):
    criterio: str
    nota: Nota
    comentario: str | None = None


@router.post("/{id}/avaliacoes", status_code=201)
def avaliar(id: UUID, corpo: NovaAvaliacao, usuario: UsuarioAtual) -> AvaliacaoDTO:
    return servico.registrar_avaliacao(id, corpo.criterio, corpo.nota, corpo.comentario, usuario)


class Descarte(BaseModel):
    motivo: str


@router.post("/{id}/proximos-passos/{pid}/descarte")
def descartar(id: UUID, pid: UUID, corpo: Descarte, usuario: Curador) -> ProximoPassoDTO:
    if pid not in {p.id for p in servico.proximos_passos(id)}:
        raise NaoEncontrado(f"Próximo passo {pid} não encontrado no projeto {id}")
    return servico.descartar_proximo_passo(pid, corpo.motivo, usuario.nome)
