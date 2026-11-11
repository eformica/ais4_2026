"""DTOs compartilhados entre módulos (dataclasses congeladas).

Os serviços devolvem estes objetos, montados dentro do `with sessao(...)`; nunca
instâncias ORM. Cada módulo só conhece os DTOs, não os modelos dos outros.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True)
class ArtefatoRef:
    uri: str
    sha256: str
    nome: str
    tipo: str


@dataclass(frozen=True)
class VersaoPlaybookDTO:
    playbook_id: UUID
    codigo: str
    versao: str
    status: str


@dataclass(frozen=True)
class IdeiaDTO:
    """Espelha as colunas de `acervo.ideia` (sem `excluido_em`)."""

    id: UUID
    titulo: str
    tipo: str
    descricao: str
    pergunta_norteadora: str
    dominio: list[str]
    origem: str
    origem_url: str | None
    playbook_sugerido_id: UUID | None
    fontes_candidatas: list[dict]
    restricoes: str | None
    valor: int
    viabilidade: int
    prioridade_manual: Decimal | None
    prioridade: Decimal
    status: str
    motivo_status: str | None
    fundida_em_id: UUID | None
    criado_por: str
    criado_em: datetime
    atualizado_em: datetime


@dataclass(frozen=True)
class ProjetoDTO:
    """Espelha as colunas de `projetos.projeto` (sem `excluido_em`)."""

    id: UUID
    titulo: str
    ideia_origem_id: UUID
    playbook_id: UUID
    playbook_versao: str
    status: str
    motivo_status: str | None
    dono: str
    criado_em: datetime
    atualizado_em: datetime


@dataclass(frozen=True)
class MarcoDTO:
    """Espelha as colunas de `projetos.marco`."""

    id: UUID
    projeto_id: UUID
    codigo: str
    ordem: int
    nome: str
    etapa_codigo: str | None
    criterio_saida: str
    exige_gate: bool
    papel_revisor: str | None
    revisor: str | None
    status: str
    data_planejada: date | None
    data_real: date | None
    decisao_gate: str | None
    decidido_em: datetime | None
