"""Ideia piloto (a mesma de `db/sql/04_seed.sql`) como DTO, para testes de qualquer módulo."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from ais4.comum.dto import IdeiaDTO

_NOMES_FONTES = [
    "IBGE - Censo 2022",
    "IBGE - Projeções da População",
    "IBGE - Produção Agrícola Municipal (PAM)",
    "IBGE - Pesquisa da Pecuária Municipal (PPM)",
    "IBGE - Pesquisa de Orçamentos Familiares (POF)",
    "IBGE - Censo Agropecuário",
    "Conab - safras e armazenagem",
    "MAPA - Projeções do Agronegócio",
    "Comex Stat",
    "Ministério dos Transportes / ONTL",
]

_AGORA = datetime(2026, 11, 11, tzinfo=UTC)

IDEIA_SEMENTE = IdeiaDTO(
    id=UUID("00000000-0000-4000-8000-000000000101"),
    titulo="Demografia, produção agropecuária e abastecimento no Brasil (2020–2050)",
    tipo="pergunta_de_pesquisa",
    descricao=(
        "O abastecimento com alimentos de qualidade depende do volume produzido, mas também de "
        "onde se produz, onde a população vive e da capacidade de transportar e armazenar. "
        "Mudanças demográficas e na produção até 2050 podem alterar demanda, rotas e "
        "disponibilidade por região."
    ),
    pergunta_norteadora=(
        "Como as mudanças demográficas e a evolução projetada da produção agropecuária entre "
        "2020 e 2050 podem afetar a disponibilidade e a distribuição regional de alimentos nos "
        "centros consumidores?"
    ),
    dominio=["agro", "demografia", "logistica", "seguranca_alimentar"],
    origem="humana",
    origem_url=None,
    playbook_sugerido_id=UUID("00000000-0000-4000-8000-000000000001"),
    fontes_candidatas=[{"nome": nome} for nome in _NOMES_FONTES],
    restricoes=(
        "Horizonte 2050; projeções tratadas como cenários; consumo regional estimado por proxy."
    ),
    valor=4,
    viabilidade=3,
    prioridade_manual=None,
    prioridade=Decimal(12),
    status="triagem",
    motivo_status=None,
    fundida_em_id=None,
    criado_por="semente",
    criado_em=_AGORA,
    atualizado_em=_AGORA,
)
