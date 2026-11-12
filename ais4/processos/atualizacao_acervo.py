"""Processo 1.2: revisão periódica do Acervo (relatório para decisão humana).

A periodicidade fica fora do código: um agendamento externo (Agendador de Tarefas do Windows,
cron mensal) chama `python -m ais4 revisao-acervo`. Esta revisão só **lê e reindexa**: nunca
funde, arquiva nem transiciona ideias; quem decide é o curador, com o relatório na mão.
"""

import logging
from dataclasses import dataclass, field
from uuid import UUID

from ais4.acervo import servico, similaridade
from ais4.acervo.similaridade import Embedder
from ais4.config import obter_config

log = logging.getLogger(__name__)

STATUS_REVISADOS = ("rascunho", "triagem", "qualificada")


@dataclass(frozen=True)
class RelatorioRevisao:
    """`reindexadas`: vetores refeitos (texto alterado) ou criados (ideia ainda sem vetor).
    `candidatas_fusao`: pares `(id_menor, id_maior, similaridade)` acima do limiar, sem revisão de
    duplicidade. `lacunas_reabertas`: ids das lacunas em `reabrir` aguardando o curador.
    `falhas_indexacao`: ideias cujo embedding o provedor recusou (ficam de fora da comparação)."""

    reindexadas: int
    candidatas_fusao: list[tuple[UUID, UUID, float]]
    lacunas_reabertas: list[UUID]
    falhas_indexacao: list[UUID] = field(default_factory=list)


def revisao_periodica(usuario: str, embedder: Embedder) -> RelatorioRevisao:
    """Reindexa o que mudou, lista pares parecidos não revisados e as lacunas `reabrir`."""
    alteradas = similaridade.reindexar_alteradas(embedder)
    novas = similaridade.indexar_pendentes(embedder, limite=100_000)
    limiar = obter_config().limiar_duplicidade
    pares = similaridade.pares_nao_revisados(limiar, STATUS_REVISADOS)
    lacunas = [x.id for x in servico.lacunas("reabrir")]
    relatorio = RelatorioRevisao(
        reindexadas=alteradas.indexadas + novas.indexadas,
        candidatas_fusao=pares,
        lacunas_reabertas=lacunas,
        falhas_indexacao=[*alteradas.falhas, *novas.falhas],
    )
    log.info(
        "Revisão do acervo por %s: %d reindexada(s), %d par(es) candidato(s), %d lacuna(s) reaberta(s)",
        usuario,
        relatorio.reindexadas,
        len(pares),
        len(lacunas),
    )
    return relatorio
