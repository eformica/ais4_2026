import math
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from ais4 import config
from ais4.acervo import servico, similaridade
from ais4.acervo.servico import IdeiaAtualizar, IdeiaCriar
from ais4.acervo.similaridade import (
    EmbedderFalso,
    VoyageEmbedder,
    ideias_sem_indice,
    indexar,
    indexar_pendentes,
    similares,
    texto_para_embedding,
)
from ais4.comum.dto import IdeiaDTO
from ais4.comum.erros import NaoEncontrado, RegraViolada, ServicoIndisponivel
from tests.acervo.vetores import DIMENSAO, EmbedderInstrumentado, vetor
from tests.playbooks.semente import IDEIA_SEMENTE


def _ideia(titulo="Ideia", **kw) -> IdeiaDTO:
    campos = {
        "titulo": titulo,
        "tipo": "pergunta_de_pesquisa",
        "descricao": f"Descrição de {titulo}.",
        "pergunta_norteadora": f"Pergunta de {titulo}?",
        "dominio": ["agro"],
        "valor": 3,
        "viabilidade": 3,
    }
    campos.update(kw)
    return servico.criar_ideia(IdeiaCriar(**campos), "ana")


def _embedder(*pares: tuple[IdeiaDTO, list[float]]) -> EmbedderFalso:
    return EmbedderFalso({texto_para_embedding(i): v for i, v in pares})


class EmbedderContador(EmbedderFalso):
    def __init__(self):
        super().__init__()
        self.chamadas = 0

    def embed(self, textos):
        self.chamadas += 1
        return super().embed(textos)


# --- texto e embedders -------------------------------------------------------------------------
def test_texto_para_embedding_junta_titulo_pergunta_e_descricao():
    s = IDEIA_SEMENTE
    assert texto_para_embedding(s) == s.titulo + "\n" + s.pergunta_norteadora + "\n" + s.descricao


def test_embedder_falso_padrao_e_deterministico_unitario_e_1024():
    e = EmbedderFalso()
    a1, a2, b = e.embed(["texto a", "texto a", "texto b"])
    assert a1 == a2
    assert a1 != b
    assert len(a1) == e.dimensao == DIMENSAO
    assert math.isclose(math.sqrt(sum(x * x for x in a1)), 1.0, rel_tol=1e-9)
    assert e.modelo


def test_embedder_falso_usa_vetor_mapeado():
    mapeado = vetor(1.0)
    e = EmbedderFalso({"x": mapeado})
    assert e.embed(["x"]) == [mapeado]
    assert e.embed(["y"])[0] != mapeado  # fora do mapa: derivado do hash


def test_voyage_embedder_usa_cliente_com_modelo_e_tipo_documento(monkeypatch):
    chamadas = {}

    class ClienteFalso:
        def __init__(self, api_key=None, **kw):
            chamadas["api_key"] = api_key

        def embed(self, texts, model=None, input_type=None, **kw):
            chamadas.update(textos=texts, model=model, input_type=input_type)
            return SimpleNamespace(embeddings=[[0.5] * DIMENSAO for _ in texts])

    monkeypatch.setattr(similaridade.voyageai, "Client", ClienteFalso)
    e = VoyageEmbedder(modelo="modelo-x", api_key="chave-y")
    assert e.modelo == "modelo-x"
    assert e.dimensao == DIMENSAO
    assert e.embed(["a", "b"]) == [[0.5] * DIMENSAO] * 2
    assert chamadas == {
        "api_key": "chave-y",
        "textos": ["a", "b"],
        "model": "modelo-x",
        "input_type": "document",
    }


def test_voyage_embedder_defaults_vem_da_config(monkeypatch):
    monkeypatch.setenv("AIS4_EMBEDDING_MODELO", "modelo-config")
    monkeypatch.setenv("AIS4_VOYAGE_API_KEY", "chave-config")
    config.obter_config.cache_clear()
    try:
        e = VoyageEmbedder()
        assert e.modelo == "modelo-config"
        assert e.api_key == "chave-config"
    finally:
        monkeypatch.undo()
        config.obter_config.cache_clear()


# --- indexar --------------------------------------------------------------------------------
def test_indexar_grava_vetor_modelo_e_hash(banco, limpar):
    a = _ideia("a")
    indexar(a.id, _embedder((a, vetor(1.0))))
    with psycopg.connect(banco) as c:
        linhas = c.execute(
            "select modelo, conteudo_sha256, vector_dims(embedding) from acervo.ideia_embedding "
            "where ideia_id = %s",
            [a.id],
        ).fetchall()
    assert len(linhas) == 1
    assert linhas[0][0] == EmbedderFalso().modelo
    assert len(linhas[0][1]) == 64
    assert linhas[0][2] == DIMENSAO


def test_indexar_nao_refaz_se_conteudo_igual(banco, limpar):
    a = _ideia("a")
    emb = EmbedderContador()
    indexar(a.id, emb)
    indexar(a.id, emb)
    assert emb.chamadas == 1
    # campo que não entra no texto não reindexa
    servico.atualizar(a.id, IdeiaAtualizar(valor=5), "ana")
    indexar(a.id, emb)
    assert emb.chamadas == 1
    # texto mudou: reindexa e troca o hash
    with psycopg.connect(banco) as c:
        antes = c.execute("select conteudo_sha256 from acervo.ideia_embedding").fetchone()[0]
    servico.atualizar(a.id, IdeiaAtualizar(descricao="Outra descrição."), "ana")
    indexar(a.id, emb)
    assert emb.chamadas == 2
    with psycopg.connect(banco) as c:
        linhas = c.execute("select conteudo_sha256 from acervo.ideia_embedding").fetchall()
    assert len(linhas) == 1
    assert linhas[0][0] != antes


def test_indexar_refaz_se_modelo_mudou(banco, limpar):
    a = _ideia("a")
    primeiro, segundo = EmbedderContador(), EmbedderContador()
    segundo.modelo = "outro-modelo"
    indexar(a.id, primeiro)
    indexar(a.id, segundo)
    assert segundo.chamadas == 1
    with psycopg.connect(banco) as c:
        assert c.execute("select modelo from acervo.ideia_embedding").fetchall() == [
            ("outro-modelo",)
        ]


def test_indexar_rejeita_dimensao_errada(banco, limpar):
    a = _ideia("a")
    with pytest.raises(ValueError, match="1024"):
        indexar(a.id, EmbedderFalso({texto_para_embedding(a): [1.0, 0.0]}))
    with psycopg.connect(banco) as c:
        assert c.execute("select count(*) from acervo.ideia_embedding").fetchone()[0] == 0


def test_indexar_ideia_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        indexar(uuid4(), EmbedderFalso())


# --- similares ------------------------------------------------------------------------------
def test_similares_ordena_por_cosseno(banco, limpar):
    a, b, c = _ideia("a"), _ideia("b"), _ideia("c")
    emb = _embedder((a, vetor(1.0)), (b, vetor(3.0, 1.0)), (c, vetor(0.0, 1.0)))
    for i in (a, b, c):
        indexar(i.id, emb)
    resultado = similares(a.id)
    assert [i.id for i, _ in resultado] == [b.id, c.id]
    assert resultado[0][1] == pytest.approx(3 / math.sqrt(10), abs=1e-5)
    assert resultado[1][1] == pytest.approx(0.0, abs=1e-5)
    assert all(isinstance(i, IdeiaDTO) for i, _ in resultado)


def test_similares_exclui_a_propria_ideia_e_respeita_limite(banco, limpar):
    a, b, c = _ideia("a"), _ideia("b"), _ideia("c")
    emb = _embedder((a, vetor(1.0)), (b, vetor(1.0, 0.1)), (c, vetor(1.0, 0.5)))
    for i in (a, b, c):
        indexar(i.id, emb)
    assert [i.id for i, _ in similares(a.id, limite=1)] == [b.id]
    assert a.id not in [i.id for i, _ in similares(a.id, limite=10)]


def test_similares_inclui_qualquer_status_e_exclui_excluidas(banco, limpar):
    alvo, arquivada, excluida = _ideia("alvo"), _ideia("arquivada"), _ideia("excluida")
    servico.transicionar(arquivada.id, "arquivada", "carla", motivo="Fora de escopo")
    emb = _embedder((alvo, vetor(1.0)), (arquivada, vetor(1.0, 0.1)), (excluida, vetor(1.0, 0.2)))
    for i in (alvo, arquivada, excluida):
        indexar(i.id, emb)
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute("update acervo.ideia set excluido_em = now() where id = %s", [excluida.id])
    assert [i.id for i, _ in similares(alvo.id)] == [arquivada.id]


def test_similares_exige_ideia_indexada(banco, limpar):
    a = _ideia("a")
    with pytest.raises(RegraViolada, match="indexada"):
        similares(a.id)
    with pytest.raises(NaoEncontrado):
        similares(uuid4())


def test_indice_hnsw_cosseno_existe(banco, limpar):
    with psycopg.connect(banco) as c:
        definicao = c.execute(
            "select indexdef from pg_indexes where schemaname = 'acervo' "
            "and tablename = 'ideia_embedding' and indexdef ilike '%hnsw%'"
        ).fetchone()[0]
    assert "vector_cosine_ops" in definicao


# --- indexar_pendentes ------------------------------------------------------------------------
def test_indexar_pendentes_faz_uma_chamada_por_lote(banco, limpar):
    for n in range(3):
        _ideia(f"p{n}")  # + a semente = 4 pendentes
    emb = EmbedderInstrumentado()
    resultado = indexar_pendentes(emb)
    assert (resultado.indexadas, resultado.falhas) == (4, [])
    assert len(emb.chamadas) == 1
    assert len(emb.chamadas[0]) == 4
    assert ideias_sem_indice() == []
    assert indexar_pendentes(emb).indexadas == 0
    assert len(emb.chamadas) == 1  # nada pendente: não chama o provedor


def test_indexar_pendentes_respeita_limite_e_tamanho_do_lote(banco, limpar, monkeypatch):
    monkeypatch.setattr(similaridade, "TAMANHO_LOTE", 2)
    for n in range(4):
        _ideia(f"p{n}")  # + semente = 5
    emb = EmbedderInstrumentado()
    resultado = indexar_pendentes(emb, limite=3)
    assert resultado.indexadas == 3
    assert [len(c) for c in emb.chamadas] == [2, 1]
    assert len(ideias_sem_indice()) == 2


def test_indexar_pendentes_isola_ideia_problematica(banco, limpar):
    boas = [_ideia(f"boa{n}") for n in range(2)]
    ruim = _ideia("VENENO")
    emb = EmbedderInstrumentado(venenos=("VENENO",))
    resultado = indexar_pendentes(emb)
    assert resultado.falhas == [ruim.id]
    assert resultado.indexadas == 3  # semente + 2 boas
    assert set(ideias_sem_indice()) == {ruim.id}
    assert all(b.id not in ideias_sem_indice() for b in boas)
    assert len(emb.chamadas[0]) == 4  # 1ª tentativa em lote; depois item a item


def test_indexar_pendentes_dimensao_errada_vai_para_falhas(banco, limpar):
    ruim = _ideia("curta")
    emb = EmbedderFalso({texto_para_embedding(ruim): [1.0, 0.0]})
    resultado = indexar_pendentes(emb)
    assert resultado.falhas == [ruim.id]
    assert resultado.indexadas == 1


def test_indexar_falha_do_provedor_vira_servico_indisponivel(banco, limpar):
    a = _ideia("VENENO")
    with pytest.raises(ServicoIndisponivel):
        indexar(a.id, EmbedderInstrumentado(venenos=("VENENO",)))
    assert a.id in ideias_sem_indice()
