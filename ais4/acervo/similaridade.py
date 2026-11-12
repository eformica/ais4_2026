"""Similaridade entre ideias: porta `Embedder` (Voyage AI / falso), indexação e busca (pgvector).

O texto de uma ideia é `titulo + pergunta_norteadora + descricao`; o vetor tem 1024 dimensões
(D11) e fica em `acervo.ideia_embedding`. A similaridade é `1 - distância cosseno`.
"""

import hashlib
import logging
import random
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import voyageai
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from ais4.acervo import servico
from ais4.acervo.modelos import EmbeddingModelo, IdeiaModelo, RevisaoDuplicidadeModelo
from ais4.comum.dto import IdeiaDTO
from ais4.comum.erros import RegraViolada, ServicoIndisponivel
from ais4.config import obter_config
from ais4.db import sessao

DIMENSAO = 1024
TAMANHO_LOTE = 32  # textos por chamada de embed
LIMITE_PENDENTES = 100  # ideias sem vetor indexadas por chamada de `indexar_pendentes`

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    modelo: str
    dimensao: int

    def embed(self, textos: list[str]) -> list[list[float]]: ...


class VoyageEmbedder:
    """Embeddings da Voyage AI. O cliente só é criado na primeira chamada de `embed`."""

    def __init__(self, modelo: str | None = None, api_key: str | None = None):
        cfg = obter_config()
        self.modelo = modelo or cfg.embedding_modelo
        self.api_key = api_key or cfg.voyage_api_key
        self.dimensao = DIMENSAO
        self._cliente: voyageai.Client | None = None

    def embed(self, textos: list[str]) -> list[list[float]]:
        if self._cliente is None:
            self._cliente = voyageai.Client(api_key=self.api_key)
        resposta = self._cliente.embed(textos, model=self.modelo, input_type="document")
        return [list(v) for v in resposta.embeddings]


class EmbedderFalso:
    """Para testes: devolve o vetor mapeado ou um vetor unitário determinístico do SHA-256."""

    modelo = "falso"
    dimensao = DIMENSAO

    def __init__(self, vetores: dict[str, list[float]] | None = None):
        self.vetores = dict(vetores or {})

    def embed(self, textos: list[str]) -> list[list[float]]:
        return [self.vetores[t] if t in self.vetores else self._derivar(t) for t in textos]

    def _derivar(self, texto: str) -> list[float]:
        gerador = random.Random(hashlib.sha256(texto.encode("utf-8")).digest())
        v = [gerador.gauss(0.0, 1.0) for _ in range(self.dimensao)]
        norma = sum(x * x for x in v) ** 0.5
        return [x / norma for x in v]


def texto_para_embedding(ideia: IdeiaDTO) -> str:
    return ideia.titulo + "\n" + ideia.pergunta_norteadora + "\n" + ideia.descricao


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _validar_dimensao(vetor: list[float]) -> None:
    if len(vetor) != DIMENSAO:
        raise ValueError(f"Embedding com {len(vetor)} dimensões; a coluna exige {DIMENSAO}")


def _gravar_vetores(s, linhas: list[dict]) -> None:
    """Upsert de vários vetores na transação `s`."""
    inserir = pg_insert(EmbeddingModelo.__table__).values(linhas)
    s.execute(
        inserir.on_conflict_do_update(
            index_elements=["ideia_id"],
            set_={
                "modelo": inserir.excluded.modelo,
                "conteudo_sha256": inserir.excluded.conteudo_sha256,
                "embedding": inserir.excluded.embedding,
                "gerado_em": func.now(),
            },
        )
    )


def indexar(ideia_id: UUID, embedder: Embedder) -> None:
    """Gera e grava o vetor da ideia; não refaz se texto e modelo não mudaram.

    Falha do provedor de embeddings vira `ServicoIndisponivel`; dimensão errada, `ValueError`.
    """
    ideia = servico.obter(ideia_id)
    texto = texto_para_embedding(ideia)
    sha = _sha(texto)
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        atual = s.get(EmbeddingModelo, ideia_id)
        if atual is not None and atual.conteudo_sha256 == sha and atual.modelo == embedder.modelo:
            return
    try:
        [vetor] = embedder.embed([texto])
    except Exception as exc:
        raise ServicoIndisponivel(
            f"Serviço de embeddings indisponível ao indexar a ideia {ideia_id}"
        ) from exc
    _validar_dimensao(vetor)
    linha = {
        "ideia_id": ideia_id,
        "modelo": embedder.modelo,
        "conteudo_sha256": sha,
        "embedding": vetor,
    }
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        _gravar_vetores(s, [linha])


@dataclass(frozen=True)
class ResultadoIndexacao:
    indexadas: int
    falhas: list[UUID]


def _sem_indice(limite: int | None = None) -> list[IdeiaModelo]:
    consulta = (
        select(IdeiaModelo)
        .outerjoin(EmbeddingModelo, EmbeddingModelo.ideia_id == IdeiaModelo.id)
        .where(IdeiaModelo.excluido_em.is_(None), EmbeddingModelo.ideia_id.is_(None))
        .order_by(IdeiaModelo.criado_em, IdeiaModelo.id)
    )
    if limite is not None:
        consulta = consulta.limit(limite)
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        return list(s.scalars(consulta))


def ideias_sem_indice() -> list[UUID]:
    """Ids das ideias não excluídas que ainda não têm vetor."""
    return [i.id for i in _sem_indice()]


def _embedar(embedder: Embedder, textos: list[str]) -> list[list[float]]:
    vetores = embedder.embed(textos)
    if len(vetores) != len(textos):
        raise ValueError(f"{len(vetores)} embeddings para {len(textos)} textos")
    for v in vetores:
        _validar_dimensao(v)
    return vetores


def _indexar_em_lotes(pendentes: list[tuple[UUID, str]], embedder: Embedder) -> ResultadoIndexacao:
    """Gera e grava os vetores de `(ideia_id, texto)` em lotes; falha isolada vai para `falhas`."""
    indexadas = 0
    falhas: list[UUID] = []
    for inicio in range(0, len(pendentes), TAMANHO_LOTE):
        lote = pendentes[inicio : inicio + TAMANHO_LOTE]
        try:
            resultados = list(zip(lote, _embedar(embedder, [t for _, t in lote]), strict=True))
        except Exception:  # noqa: BLE001  (qualquer falha do provedor: isola item a item)
            resultados = []
            for item in lote:
                try:
                    resultados.append((item, _embedar(embedder, [item[1]])[0]))
                except Exception:
                    logger.warning("Falha ao gerar embedding da ideia %s", item[0], exc_info=True)
                    falhas.append(item[0])
        linhas = [
            {
                "ideia_id": id_,
                "modelo": embedder.modelo,
                "conteudo_sha256": _sha(texto),
                "embedding": vetor,
            }
            for (id_, texto), vetor in resultados
        ]
        if linhas:
            with sessao("acervo", servico.USUARIO_SISTEMA) as s:
                _gravar_vetores(s, linhas)
            indexadas += len(linhas)
    return ResultadoIndexacao(indexadas, falhas)


def indexar_pendentes(embedder: Embedder, limite: int = LIMITE_PENDENTES) -> ResultadoIndexacao:
    """Indexa até `limite` ideias sem vetor, em lotes; cada lote grava em sua própria transação.

    Se o lote falha, tenta item a item para isolar a ideia problemática: ela entra em `falhas`
    (e é registrada em log). Falhas do provedor nunca propagam.
    """
    pendentes = [(i.id, texto_para_embedding(servico._dto(i))) for i in _sem_indice(limite)]
    return _indexar_em_lotes(pendentes, embedder)


def reindexar_alteradas(embedder: Embedder) -> ResultadoIndexacao:
    """Refaz o vetor das ideias cujo texto (ou o modelo de embedding) mudou desde a indexação.

    Compara o SHA-256 do texto atual com `conteudo_sha256`; ideias sem vetor ficam com
    `indexar_pendentes`. Mesmas garantias de lote e isolamento de falha dessa função.
    """
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        linhas = s.execute(
            select(IdeiaModelo, EmbeddingModelo.conteudo_sha256, EmbeddingModelo.modelo)
            .join(EmbeddingModelo, EmbeddingModelo.ideia_id == IdeiaModelo.id)
            .where(IdeiaModelo.excluido_em.is_(None))
            .order_by(IdeiaModelo.criado_em, IdeiaModelo.id)
        ).all()
        alteradas = []
        for ideia, sha, modelo in linhas:
            texto = texto_para_embedding(servico._dto(ideia))
            if _sha(texto) != sha or modelo != embedder.modelo:
                alteradas.append((ideia.id, texto))
    return _indexar_em_lotes(alteradas, embedder)


def similares(ideia_id: UUID, limite: int = 5) -> list[tuple[IdeiaDTO, float]]:
    """Ideias mais parecidas com a pedida, da mais para a menos similar.

    Inclui ideias de qualquer status (as `em_projeto` e `concluida` são projetos existentes);
    exclui a própria ideia e as excluídas. A ideia precisa estar indexada.
    """
    servico.obter(ideia_id)  # NaoEncontrado se não existir
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        alvo = s.get(EmbeddingModelo, ideia_id)
        if alvo is None:
            raise RegraViolada(f"Ideia {ideia_id} ainda não foi indexada")
        distancia = EmbeddingModelo.embedding.cosine_distance(alvo.embedding)
        linhas = s.execute(
            select(IdeiaModelo, distancia)
            .join(EmbeddingModelo, EmbeddingModelo.ideia_id == IdeiaModelo.id)
            .where(IdeiaModelo.id != ideia_id, IdeiaModelo.excluido_em.is_(None))
            .order_by(distancia, IdeiaModelo.criado_em)
            .limit(limite)
        ).all()
        return [(servico._dto(ideia), 1.0 - float(d)) for ideia, d in linhas]


def similaridade_entre(ideia_id: UUID, outra_id: UUID) -> float:
    """Similaridade cosseno entre duas ideias já indexadas."""
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        a, b = s.get(EmbeddingModelo, ideia_id), s.get(EmbeddingModelo, outra_id)
        if a is None or b is None:
            faltante = ideia_id if a is None else outra_id
            raise RegraViolada(f"Ideia {faltante} ainda não foi indexada")
        distancia = s.scalar(
            select(EmbeddingModelo.embedding.cosine_distance(b.embedding)).where(
                EmbeddingModelo.ideia_id == ideia_id
            )
        )
        return 1.0 - float(distancia)


def pares_nao_revisados(
    limiar: float, status: tuple[str, ...] = ("rascunho", "triagem", "qualificada")
) -> list[tuple[UUID, UUID, float]]:
    """Pares de ideias indexadas com similaridade >= `limiar` que ninguém revisou ainda.

    Cada par não ordenado aparece uma vez, com o menor id primeiro, da maior para a menor
    similaridade. Só entram ideias não excluídas nos `status` pedidos; um par com qualquer decisão
    em `ideia_revisao_duplicidade` (nos dois sentidos) já foi revisado e não entra.
    """
    a, b = aliased(EmbeddingModelo), aliased(EmbeddingModelo)
    ia, ib = aliased(IdeiaModelo), aliased(IdeiaModelo)
    distancia = a.embedding.cosine_distance(b.embedding)
    revisado = (
        select(1)
        .where(
            or_(
                and_(
                    RevisaoDuplicidadeModelo.ideia_id == a.ideia_id,
                    RevisaoDuplicidadeModelo.candidata_id == b.ideia_id,
                ),
                and_(
                    RevisaoDuplicidadeModelo.ideia_id == b.ideia_id,
                    RevisaoDuplicidadeModelo.candidata_id == a.ideia_id,
                ),
            )
        )
        .exists()
    )
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        linhas = s.execute(
            select(a.ideia_id, b.ideia_id, distancia)
            .join(b, a.ideia_id < b.ideia_id)
            .join(ia, ia.id == a.ideia_id)
            .join(ib, ib.id == b.ideia_id)
            .where(
                ia.excluido_em.is_(None),
                ib.excluido_em.is_(None),
                ia.status.in_(status),
                ib.status.in_(status),
                distancia <= 1.0 - limiar,
                ~revisado,
            )
            .order_by(distancia, a.ideia_id, b.ideia_id)
        ).all()
        return [(x, y, 1.0 - float(d)) for x, y, d in linhas]
