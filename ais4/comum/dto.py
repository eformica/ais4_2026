"""DTOs compartilhados entre módulos (dataclasses congeladas).

Os serviços devolvem estes objetos, montados dentro do `with sessao(...)`; nunca
instâncias ORM. Cada módulo só conhece os DTOs, não os modelos dos outros.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from ais4.comum.espec import EspecProjeto


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


@dataclass(frozen=True)
class EventoDTO:
    """Espelha as colunas de `acervo.ideia_evento` (histórico da ideia)."""

    id: int
    ideia_id: UUID
    evento: str
    de_status: str | None
    para_status: str | None
    motivo: str | None
    autor: str
    ocorrido_em: datetime


@dataclass(frozen=True)
class LacunaDTO:
    """Espelha `acervo.ideia_lacuna`: ideia sem playbook ativo que a cubra."""

    id: UUID
    ideia_id: UUID
    descricao: str
    classes_requeridas: list[str]
    status: str
    criado_em: datetime
    resolvida_em: datetime | None


@dataclass(frozen=True)
class ArtefatoDTO:
    """Espelha `projetos.artefato` (metadados; o arquivo fica no armazém)."""

    id: UUID
    projeto_id: UUID
    marco_id: UUID | None
    tipo: str
    nome: str
    uri: str
    hash_sha256: str
    proveniencia: dict
    criado_em: datetime


@dataclass(frozen=True)
class AvaliacaoDTO:
    """Espelha `projetos.avaliacao`."""

    id: UUID
    projeto_id: UUID
    criterio: str
    nota: int
    comentario: str | None
    revisor: str
    avaliado_em: datetime


@dataclass(frozen=True)
class LicaoDTO:
    """Espelha `projetos.licao_aprendida`."""

    id: UUID
    projeto_id: UUID
    tipo: str
    descricao: str
    playbook_alvo_id: UUID | None
    encaminhada_em: datetime | None
    criado_em: datetime


@dataclass(frozen=True)
class ProximoPassoDTO:
    """Espelha `projetos.proximo_passo`."""

    id: UUID
    projeto_id: UUID
    descricao: str
    ideia_gerada_id: UUID | None
    descartado_motivo: str | None
    criado_em: datetime


@dataclass(frozen=True)
class EspecificacaoDTO:
    """Uma versão da especificação do projeto (`projetos.especificacao`), já interpretada."""

    projeto_id: UUID
    versao: int
    especificacao: EspecProjeto
    justificativa: str | None
    aprovado_por: str
    aprovado_em: datetime


@dataclass(frozen=True)
class PropostaDTO:
    """Espelha as colunas de `criacao.proposta`; `conteudo` é a `EspecProjeto` em JSON."""

    id: UUID
    ideia_id: UUID
    playbook_id: UUID | None
    playbook_versao: str | None
    status: str
    conteudo: dict | None
    iteracoes: int
    motivo: str | None
    solicitado_por: str
    decidido_por: str | None
    decidido_em: datetime | None
    projeto_id: UUID | None
    criado_em: datetime
    atualizado_em: datetime


@dataclass(frozen=True)
class IteracaoDTO:
    """Uma rodada da autocrítica (ou um ajuste humano) em `criacao.proposta_iteracao`."""

    proposta_id: UUID
    n: int
    rascunho: dict
    lacunas: list[dict]
    aprovada_pela_critica: bool
    custo_usd: Decimal
    criado_em: datetime
