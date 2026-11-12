from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ais4.auth import Usuario, exigir_papel, usuario_atual
from ais4.comum.dto import VersaoPlaybookDTO
from ais4.comum.erros import RegraViolada
from ais4.playbooks import servico
from ais4.processos import promocao

PAPEL_GUARDIAO = "guardiao_de_playbooks"

router = APIRouter(prefix="/playbooks", tags=["playbooks"], dependencies=[Depends(usuario_atual)])


class NovoStatus(BaseModel):
    status: Literal["proposto", "experimental", "validado", "depreciado"]


@router.get("")
def listar() -> list[VersaoPlaybookDTO]:
    return servico.listar_versoes()


@router.get("/{codigo}/versoes/{versao}")
def detalhar(codigo: str, versao: str) -> dict:
    dto = servico.obter_versao(codigo, versao)
    return {
        "versao": asdict(dto),
        "etapas": [asdict(e) for e in servico.etapas(dto.playbook_id, versao)],
    }


@router.post("/{codigo}/versoes/{versao}/status")
def alterar_status(
    codigo: str,
    versao: str,
    corpo: NovoStatus,
    usuario: Annotated[Usuario, Depends(usuario_atual)],
) -> VersaoPlaybookDTO:
    if corpo.status == "validado":
        raise RegraViolada(
            "A promoção para 'validado' confere os limiares (6.2): use "
            f"POST /playbooks/{codigo}/versoes/{versao}/promocao"
        )
    dto = servico.obter_versao(codigo, versao)
    servico.alterar_status(dto.playbook_id, versao, corpo.status, usuario)
    return servico.obter_versao(codigo, versao)


@router.post("/{codigo}/versoes/{versao}/promocao")
def promover(
    codigo: str,
    versao: str,
    usuario: Annotated[Usuario, Depends(exigir_papel(PAPEL_GUARDIAO))],
) -> dict:
    """`experimental -> validado` (guardião), se os limiares de promoção foram atingidos (6.2).

    Elegibilidade primeiro (409 com o motivo); depois `alterar_status`, que aplica a regra de que
    quem propôs a versão não a promove.
    """
    dto = servico.obter_versao(codigo, versao)
    if dto.status != "experimental":
        raise RegraViolada(
            f"Só versões experimentais são promovidas; a versão {versao} está {dto.status!r}"
        )
    elegibilidade = promocao.elegibilidade(dto.playbook_id, versao)
    if not elegibilidade.elegivel:
        raise RegraViolada(elegibilidade.motivo)
    servico.alterar_status(dto.playbook_id, versao, "validado", usuario)
    return {"versao": servico.obter_versao(codigo, versao), "elegibilidade": elegibilidade}
