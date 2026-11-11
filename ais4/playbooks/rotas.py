from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ais4.auth import Usuario, usuario_atual
from ais4.comum.dto import VersaoPlaybookDTO
from ais4.playbooks import servico

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
    dto = servico.obter_versao(codigo, versao)
    servico.alterar_status(dto.playbook_id, versao, corpo.status, usuario)
    return servico.obter_versao(codigo, versao)
