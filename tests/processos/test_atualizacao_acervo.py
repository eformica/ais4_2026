import psycopg

from ais4.acervo import servico, similaridade
from ais4.acervo.servico import IdeiaAtualizar, IdeiaCriar
from ais4.acervo.similaridade import EmbedderFalso, texto_para_embedding
from ais4.auth import Usuario
from ais4.comum.dto import IdeiaDTO
from ais4.processos.atualizacao_acervo import RelatorioRevisao, revisao_periodica
from tests.acervo.vetores import EmbedderInstrumentado, vetor

CURADORA = Usuario("carla", frozenset({"curador_do_acervo"}))


def _ideia(titulo, **kw) -> IdeiaDTO:
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


def _embedder(*pares) -> EmbedderFalso:
    return EmbedderFalso({texto_para_embedding(i): v for i, v in pares})


def _contar(banco, sql):
    with psycopg.connect(banco) as c:
        return c.execute(sql).fetchone()[0]


def test_revisao_periodica_lista_candidatas_sem_fundir(limpar, banco):
    a, b, c = _ideia("A"), _ideia("B", status="triagem"), _ideia("C")
    emb = _embedder((a, vetor(1.0)), (b, vetor(1.0, 0.05)), (c, vetor(0.0, 1.0)))

    r = revisao_periodica("sistema:revisao", emb)

    assert isinstance(r, RelatorioRevisao)
    assert r.reindexadas == 4  # as 3 ideias novas e a da semente, ainda sem vetor
    [(x, y, sim)] = r.candidatas_fusao
    assert {x, y} == {a.id, b.id} and x < y and sim > 0.95
    # nada foi fundido, arquivado nem relacionado
    assert [servico.obter(i.id).status for i in (a, b, c)] == ["rascunho", "triagem", "rascunho"]
    assert _contar(banco, "select count(*) from acervo.ideia_relacao") == 0
    assert (
        _contar(banco, "select count(*) from acervo.ideia where status in ('fundida','arquivada')")
        == 0
    )


def test_revisao_periodica_nao_lista_pares_revisados_nem_de_ideias_em_projeto(limpar):
    a, b = _ideia("A"), _ideia("B")
    c = _ideia("C", fontes_candidatas=[{"nome": "IBGE"}])
    emb = _embedder((a, vetor(1.0)), (b, vetor(1.0, 0.05)), (c, vetor(1.0, 0.1)))
    servico.registrar_revisao_duplicidade(a.id, b.id, 0.99, "distinta", "carla")
    servico.transicionar(c.id, "triagem", "ana")
    servico.transicionar(c.id, "qualificada", "carla")
    servico.transicionar(c.id, "em_projeto", "sistema")

    r = revisao_periodica("sistema:revisao", emb)

    assert r.candidatas_fusao == []


def test_revisao_periodica_reindexa_embeddings_alterados(limpar, banco):
    a = _ideia("A")
    emb = EmbedderInstrumentado()
    similaridade.indexar(a.id, emb)
    servico.atualizar(a.id, IdeiaAtualizar(descricao="Texto totalmente novo."), "ana")
    emb.chamadas.clear()

    r = revisao_periodica("sistema:revisao", emb)

    textos = [t for chamada in emb.chamadas for t in chamada]
    assert "Texto totalmente novo." in "".join(textos)
    assert r.reindexadas >= 1 and r.falhas_indexacao == []
    assert revisao_periodica("sistema:revisao", emb).reindexadas == 0  # segunda volta: nada mudou


def test_revisao_periodica_lista_lacunas_reabertas(limpar):
    ideia = _ideia("A")
    reabrir = servico.registrar_lacuna(ideia.id, "sem playbook", ["inferencia"], CURADORA)
    aberta = servico.registrar_lacuna(ideia.id, "outra", ["geo"], CURADORA)
    servico.marcar_lacunas_para_reabrir(["inferencia"], "sistema")

    r = revisao_periodica("sistema:revisao", EmbedderFalso())

    assert r.lacunas_reabertas == [reabrir]
    assert aberta not in r.lacunas_reabertas
    assert [li.status for li in servico.lacunas("reabrir")] == ["reabrir"]  # segue com o curador


def test_revisao_periodica_com_falha_do_provedor_relata_e_segue(limpar):
    a = _ideia("A VENENO")
    _ideia("B")
    emb = EmbedderInstrumentado(venenos=("VENENO",))

    r = revisao_periodica("sistema:revisao", emb)

    assert r.falhas_indexacao == [a.id]
