import logging

import psycopg
import pytest

from ais4.acervo import servico, similaridade
from ais4.acervo.servico import IdeiaCriar
from ais4.acervo.similaridade import EmbedderFalso, texto_para_embedding
from ais4.comum.dto import IdeiaDTO
from ais4.comum.erros import RegraViolada, ServicoIndisponivel
from ais4.llm.cliente import RespostaInvalida
from ais4.llm.falso import ClienteLLMFalso
from ais4.processos.alimentacao_acervo import (
    Enriquecimento,
    FonteSugerida,
    SugestaoEnriquecimento,
    enriquecer,
)
from tests.acervo.vetores import vetor
from tests.projetos.fabrica import PLAYBOOK_ID

CODIGO = "projeto-academico-simples"


def _ideia(titulo="Oferta de água", **kw) -> IdeiaDTO:
    campos = {
        "titulo": titulo,
        "tipo": "pergunta_de_pesquisa",
        "descricao": f"Descrição de {titulo}.",
        "pergunta_norteadora": f"Pergunta de {titulo}?",
        "dominio": ["Agro"],
        "valor": 3,
        "viabilidade": 3,
        "fontes_candidatas": [{"nome": "IBGE - Censo", "url": "https://ibge.gov.br/censo"}],
    }
    campos.update(kw)
    return servico.criar_ideia(IdeiaCriar(**campos), "ana")


def _sugestao(**kw) -> SugestaoEnriquecimento:
    campos = {
        "dominio": ["agro", "clima"],
        "fontes": [
            FonteSugerida(nome="ANA - Hidroweb", url="https://hidroweb.ana.gov.br", cobertura="BR"),
            FonteSugerida(nome="Sem endereço", url=None, cobertura=""),
        ],
        "playbook_codigo": CODIGO,
        "justificativa": "porque sim",
    }
    campos.update(kw)
    return SugestaoEnriquecimento(**campos)


def _llm(*itens) -> ClienteLLMFalso:
    return ClienteLLMFalso({"enriquecedor": list(itens)})


def test_enriquecer_acrescenta_sem_remover_dados_do_autor(limpar):
    ideia = _ideia(dominio=["Agro", "demografia"])
    cliente = _llm(
        _sugestao(
            dominio=["agro", "clima", "clima", " "],  # "agro" repete o do autor (sem caixa)
            fontes=[
                FonteSugerida(nome="ibge - censo", url=None, cobertura=""),  # repete o nome
                FonteSugerida(
                    nome="Outra", url="https://ibge.gov.br/censo", cobertura=""
                ),  # e a URL
                FonteSugerida(
                    nome="ANA - Hidroweb", url="https://hidroweb.ana.gov.br", cobertura="BR"
                ),
            ],
        )
    )

    r = enriquecer(ideia.id, cliente, EmbedderFalso(), "carla")

    assert r.dominio_sugerido == ["clima"]
    assert [f["nome"] for f in r.fontes_sugeridas] == ["ANA - Hidroweb"]
    atual = servico.obter(ideia.id)
    assert atual.dominio == ["Agro", "demografia", "clima"]  # as do autor intactas e na frente
    assert atual.fontes_candidatas[0] == {
        "nome": "IBGE - Censo",
        "url": "https://ibge.gov.br/censo",
    }
    assert [f["nome"] for f in atual.fontes_candidatas] == ["IBGE - Censo", "ANA - Hidroweb"]
    assert atual.fontes_candidatas[1]["url"] == "https://hidroweb.ana.gov.br"
    assert atual.status == ideia.status == "rascunho"
    assert (atual.titulo, atual.descricao, atual.valor) == (ideia.titulo, ideia.descricao, 3)


def test_enriquecer_chama_o_enriquecedor_so_com_saida_estruturada_e_sem_web(limpar):
    ideia = _ideia()
    cliente = _llm(_sugestao())

    enriquecer(ideia.id, cliente, EmbedderFalso(), "carla")

    [chamada] = cliente.chamadas
    assert (chamada.metodo, chamada.papel, chamada.formato) == (
        "estruturado",
        "enriquecedor",
        SugestaoEnriquecimento,
    )
    assert ideia.titulo in chamada.mensagem and CODIGO in chamada.mensagem
    assert chamada.ferramentas == []


def test_enriquecer_preenche_playbook_so_se_estiver_vazio(limpar):
    ideia = _ideia(playbook_sugerido_id=None)

    r = enriquecer(ideia.id, _llm(_sugestao()), EmbedderFalso(), "carla")

    assert r.playbook_sugerido == CODIGO
    assert servico.obter(ideia.id).playbook_sugerido_id == PLAYBOOK_ID


def test_enriquecer_nao_sobrescreve_playbook_sugerido(limpar):
    outro = servico.criar_ideia(
        IdeiaCriar(
            titulo="x",
            tipo="problema",
            descricao="d",
            pergunta_norteadora="p?",
            dominio=["a"],
            valor=1,
            viabilidade=1,
            playbook_sugerido_id=PLAYBOOK_ID,
        ),
        "ana",
    )
    # o autor já indicou um playbook; a sugestão do LLM (de outro código) não o troca
    r = enriquecer(
        outro.id, _llm(_sugestao(playbook_codigo="inexistente")), EmbedderFalso(), "carla"
    )

    assert r.playbook_sugerido is None
    assert servico.obter(outro.id).playbook_sugerido_id == PLAYBOOK_ID
    r = enriquecer(outro.id, _llm(_sugestao()), EmbedderFalso(), "carla")
    assert r.playbook_sugerido is None  # mesmo sugerindo um válido


def test_enriquecer_ignora_playbook_desconhecido(limpar):
    ideia = _ideia(playbook_sugerido_id=None)

    r = enriquecer(
        ideia.id, _llm(_sugestao(playbook_codigo="nao-existe")), EmbedderFalso(), "carla"
    )

    assert r.playbook_sugerido is None
    assert servico.obter(ideia.id).playbook_sugerido_id is None


def test_enriquecer_devolve_candidatas_de_duplicidade_acima_do_limiar(limpar):
    a = _ideia("A")
    b = _ideia("B")
    c = _ideia("C")
    emb = EmbedderFalso(
        {
            texto_para_embedding(a): vetor(1.0),
            texto_para_embedding(b): vetor(1.0, 0.05),
            texto_para_embedding(c): vetor(0.0, 1.0),
        }
    )
    similaridade.indexar(b.id, emb)
    similaridade.indexar(c.id, emb)

    r = enriquecer(a.id, _llm(_sugestao()), emb, "carla")

    assert isinstance(r, Enriquecimento)
    assert [i for i, _ in r.candidatas_duplicidade] == [b.id]
    assert r.candidatas_duplicidade[0][1] > 0.95
    assert a.id not in similaridade.ideias_sem_indice()  # a ideia enriquecida fica indexada


def test_enriquecer_repetido_nao_acrescenta_de_novo(limpar):
    ideia = _ideia()
    enriquecer(ideia.id, _llm(_sugestao()), EmbedderFalso(), "carla")

    r = enriquecer(ideia.id, _llm(_sugestao()), EmbedderFalso(), "carla")

    assert r.dominio_sugerido == [] and r.fontes_sugeridas == []
    assert len(servico.obter(ideia.id).fontes_candidatas) == 3  # a do autor + 2 sugeridas


@pytest.mark.parametrize("status", ["qualificada", "arquivada"])
def test_enriquecer_so_em_rascunho_ou_triagem(limpar, status):
    ideia = _ideia()
    servico.transicionar(ideia.id, "triagem", "ana")
    if status == "qualificada":
        servico.transicionar(ideia.id, "qualificada", "carla")
    else:
        servico.transicionar(ideia.id, "arquivada", "carla", "teste")
    cliente = _llm(_sugestao())

    with pytest.raises(RegraViolada, match="rascunho"):
        enriquecer(ideia.id, cliente, EmbedderFalso(), "carla")
    assert cliente.chamadas == []


def test_enriquecer_resposta_invalida_vira_servico_indisponivel_sem_alterar(limpar):
    ideia = _ideia()
    cliente = _llm(RespostaInvalida("schema", "json fora do schema"))

    with pytest.raises(ServicoIndisponivel):
        enriquecer(ideia.id, cliente, EmbedderFalso(), "carla")

    assert servico.obter(ideia.id) == ideia


def test_enriquecer_registra_o_uso_em_log_sem_gravar_chamada_llm(limpar, caplog, banco):
    ideia = _ideia()

    with caplog.at_level(logging.INFO, logger="ais4.processos.alimentacao_acervo"):
        enriquecer(ideia.id, _llm(_sugestao()), EmbedderFalso(), "carla")

    assert "0.001400" in caplog.text  # custo do uso padrão do cliente falso
    with psycopg.connect(banco) as c:
        assert c.execute("select count(*) from execucao.chamada_llm").fetchone()[0] == 0
