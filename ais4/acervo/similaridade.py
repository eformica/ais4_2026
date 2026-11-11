"""Similaridade entre ideias: porta `Embedder` (Voyage AI / falso), indexação e busca (pgvector).

O texto de uma ideia é `titulo + pergunta_norteadora + descricao`; o vetor tem 1024 dimensões
(D11) e fica em `acervo.ideia_embedding`. A similaridade é `1 - distância cosseno`.
"""

import hashlib
import random
from typing import Protocol
from uuid import UUID

import voyageai
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ais4.acervo import servico
from ais4.acervo.modelos import EmbeddingModelo, IdeiaModelo
from ais4.comum.dto import IdeiaDTO
from ais4.comum.erros import RegraViolada
from ais4.config import obter_config
from ais4.db import sessao

DIMENSAO = 1024


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


def indexar(ideia_id: UUID, embedder: Embedder) -> None:
    """Gera e grava o vetor da ideia; não refaz se texto e modelo não mudaram."""
    ideia = servico.obter(ideia_id)
    texto = texto_para_embedding(ideia)
    sha = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        atual = s.get(EmbeddingModelo, ideia_id)
        if atual is not None and atual.conteudo_sha256 == sha and atual.modelo == embedder.modelo:
            return
    [vetor] = embedder.embed([texto])
    if len(vetor) != DIMENSAO:
        raise ValueError(f"Embedding com {len(vetor)} dimensões; a coluna exige {DIMENSAO}")
    valores = {
        "ideia_id": ideia_id,
        "modelo": embedder.modelo,
        "conteudo_sha256": sha,
        "embedding": vetor,
    }
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        s.execute(
            pg_insert(EmbeddingModelo.__table__)
            .values(**valores)
            .on_conflict_do_update(
                index_elements=["ideia_id"], set_=valores | {"gerado_em": func.now()}
            )
        )


def indexar_pendentes(embedder: Embedder) -> int:
    """Indexa as ideias (não excluídas) que ainda não têm vetor; devolve quantas."""
    with sessao("acervo", servico.USUARIO_SISTEMA) as s:
        ids = list(
            s.scalars(
                select(IdeiaModelo.id)
                .outerjoin(EmbeddingModelo, EmbeddingModelo.ideia_id == IdeiaModelo.id)
                .where(IdeiaModelo.excluido_em.is_(None), EmbeddingModelo.ideia_id.is_(None))
            )
        )
    for id_ in ids:
        indexar(id_, embedder)
    return len(ids)


def atualizar_indice(ideia_id: UUID, embedder: Embedder) -> None:
    """Deixa a ideia indexada e atualizada e indexa as demais ainda sem vetor.

    Sem o segundo passo, uma ideia que nunca passou por aqui ficaria invisível à busca de
    duplicatas.
    """
    indexar_pendentes(embedder)
    indexar(ideia_id, embedder)


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
