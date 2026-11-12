import json
import logging
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
from uuid import UUID, uuid5

import pytest

from ais4 import config
from ais4.acervo import handlers as acervo_handlers
from ais4.acervo import servico as acervo
from ais4.comum import eventos
from ais4.comum.erros import RegraViolada
from ais4.comum.eventos import Evento
from ais4.execucao import servico as execucao
from ais4.execucao.artefatos import Armazem
from ais4.processos import retroalimentacao
from ais4.processos.retroalimentacao import (
    NAMESPACE_IDEIAS,
    id_ideia_derivada,
    processar_em_revisao,
)
from ais4.projetos import servico as projetos
from tests.playbooks.semente import IDEIA_SEMENTE
from tests.projetos.fabrica import (
    PLAYBOOK_ID,
    aprovar_todos_os_gates,
    consultar,
    ir_para_revisao,
    novo_projeto,
)

IDEIA_1 = {
    "titulo": "Estender a análise até 2060",
    "tipo": "pergunta_de_pesquisa",
    "descricao": "Repetir o estudo com horizonte maior.",
    "pergunta_norteadora": "Como a oferta de água evolui até 2060 nas bacias estudadas?",
    "dominio": ["agro", "clima"],
    "valor": 4,
    "viabilidade": 3,
}
IDEIA_2 = {**IDEIA_1, "titulo": "Incluir microdados do censo", "valor": 2, "viabilidade": 5}
NOVAS = [
    {"proximo_passo": "Estender a 2060", "acao": "ideia", "ideia": IDEIA_1},
    {
        "proximo_passo": "Refazer com microdados",
        "acao": "descartar",
        "motivo_descarte": "sem acesso",
    },
    {"proximo_passo": "Incluir o censo", "acao": "ideia", "ideia": IDEIA_2},
]
LICOES = [
    {"tipo": "funcionou", "descricao": "Harmonizar cedo", "evidencia": "etapa de harmonização"},
    {"tipo": "falhou", "descricao": "Séries incompatíveis", "evidencia": "M3 reprovado"},
    {"tipo": "sugestao", "descricao": "Pedir licença antes", "evidencia": "coleta atrasou"},
]


@pytest.fixture
def armazem(tmp_path):
    return Armazem(tmp_path / "artefatos")


def concluir_retroalimentacao(projeto_id, armazem, novas=NOVAS, licoes=LICOES):
    """Uma tentativa `concluida` da etapa `retroalimentacao` com os artefatos no armazém."""
    t = execucao.criar_tarefa(projeto_id, "retroalimentacao", "curador", "modelo", {}, 7)
    assert execucao.reservar(t.id, 60)
    execucao.finalizar(t.id, "validando")
    saidas = {}
    for nome, dados in (("novas_ideias", novas), ("licoes_aprendidas", licoes)):
        if dados is None:
            continue
        bruto = dados if isinstance(dados, bytes) else json.dumps(dados).encode()
        uri, sha = armazem.salvar(bruto)
        saidas[nome] = {"uri": uri, "sha256": sha}
    return execucao.finalizar(t.id, "concluida", saidas=saidas)


@pytest.fixture
def projeto_em_revisao(limpar):
    p = novo_projeto()
    aprovar_todos_os_gates(p.id)
    ir_para_revisao(p.id)
    return p


def evento_em_revisao(projeto_id) -> Evento:
    return Evento(
        "projetos",
        1,
        projeto_id,
        "ProjetoStatusAlterado",
        {"de": "em_execucao", "para": "em_revisao", "ideia_origem_id": str(IDEIA_SEMENTE.id)},
    )


def ideias_derivadas(banco):
    return consultar(
        banco,
        "select titulo, status, origem, criado_por from acervo.ideia "
        "where origem = 'retroalimentacao' order by titulo",
    )


def test_id_ideia_derivada_e_deterministico():
    assert NAMESPACE_IDEIAS == UUID("6f1c2a4e-0000-4000-8000-a15400000001")
    pp = UUID("00000000-0000-4000-8000-0000000000aa")
    assert id_ideia_derivada(pp) == uuid5(NAMESPACE_IDEIAS, str(pp))


def test_2_2_gera_ideias_com_origem_retroalimentacao(projeto_em_revisao, armazem, banco):
    concluir_retroalimentacao(projeto_em_revisao.id, armazem)

    processar_em_revisao(evento_em_revisao(projeto_em_revisao.id), armazem)

    passos = {p.descricao: p for p in projetos.proximos_passos(projeto_em_revisao.id)}
    assert set(passos) == {"Estender a 2060", "Refazer com microdados", "Incluir o censo"}
    assert passos["Refazer com microdados"].descartado_motivo == "sem acesso"
    assert passos["Refazer com microdados"].ideia_gerada_id is None
    assert ideias_derivadas(banco) == [
        ("Estender a análise até 2060", "rascunho", "retroalimentacao", "sistema:retroalimentacao"),
        ("Incluir microdados do censo", "rascunho", "retroalimentacao", "sistema:retroalimentacao"),
    ]
    primeira = passos["Estender a 2060"]
    assert primeira.ideia_gerada_id == id_ideia_derivada(primeira.id)
    ideia = acervo.obter(primeira.ideia_gerada_id)
    assert (ideia.dominio, ideia.valor, ideia.viabilidade) == (["agro", "clima"], 4, 3)
    assert ideia.fontes_candidatas == [] and ideia.tipo == "pergunta_de_pesquisa"
    segunda = passos["Incluir o censo"]
    assert segunda.ideia_gerada_id == id_ideia_derivada(segunda.id)


def test_2_2_reprocessado_nao_duplica(projeto_em_revisao, armazem, banco, monkeypatch):
    """Review Focus 4: queda entre criar a ideia derivada e vinculá-la ao próximo passo."""
    concluir_retroalimentacao(projeto_em_revisao.id, armazem)
    evento = evento_em_revisao(projeto_em_revisao.id)
    vincular = projetos.vincular_ideia_gerada
    chamadas = []

    def vincular_que_cai_uma_vez(passo_id, ideia_id):
        chamadas.append(passo_id)
        if len(chamadas) == 1:
            raise RuntimeError("queda entre criar a ideia e vincular")
        vincular(passo_id, ideia_id)

    monkeypatch.setattr(projetos, "vincular_ideia_gerada", vincular_que_cai_uma_vez)

    with pytest.raises(RuntimeError):
        processar_em_revisao(evento, armazem)
    assert len(ideias_derivadas(banco)) == 1  # a ideia já existe, ainda sem vínculo

    processar_em_revisao(evento, armazem)
    processar_em_revisao(evento, armazem)  # uma terceira vez, sem efeito

    passos = projetos.proximos_passos(projeto_em_revisao.id)
    assert len(passos) == len(NOVAS)
    assert len(ideias_derivadas(banco)) == sum(1 for n in NOVAS if n["acao"] == "ideia")
    assert all(p.ideia_gerada_id or p.descartado_motivo for p in passos)
    assert len(projetos.licoes(projeto_em_revisao.id)) == len(LICOES)
    assert consultar(banco, "select count(*) from playbooks.proposta_melhoria")[0][0] == 2


def test_2_2_licoes_viram_propostas_de_melhoria(projeto_em_revisao, armazem, banco):
    concluir_retroalimentacao(projeto_em_revisao.id, armazem)

    processar_em_revisao(evento_em_revisao(projeto_em_revisao.id), armazem)

    licoes = {li.tipo: li for li in projetos.licoes(projeto_em_revisao.id)}
    assert set(licoes) == {"funcionou", "falhou", "sugestao"}
    assert all(li.playbook_alvo_id == PLAYBOOK_ID for li in licoes.values())
    assert "M3 reprovado" in licoes["falhou"].descricao  # a evidência acompanha a lição
    assert licoes["funcionou"].encaminhada_em is None
    assert licoes["falhou"].encaminhada_em and licoes["sugestao"].encaminhada_em
    propostas = consultar(
        banco,
        "select playbook_id, versao_base, origem_licao_id, origem_projeto_id, descricao, status "
        "from playbooks.proposta_melhoria",
    )
    assert {p[2] for p in propostas} == {licoes["falhou"].id, licoes["sugestao"].id}
    for playbook_id, versao, _licao, projeto_id, descricao, status in propostas:
        assert (playbook_id, versao, projeto_id, status) == (
            PLAYBOOK_ID,
            "0.1.0",
            projeto_em_revisao.id,
            "pendente",
        )
        assert "Evidência:" in descricao


def test_2_2_usa_a_ultima_tentativa_concluida(projeto_em_revisao, armazem, banco):
    concluir_retroalimentacao(projeto_em_revisao.id, armazem, novas=NOVAS[:1], licoes=LICOES[:1])
    outra = [{"proximo_passo": "Só este", "acao": "descartar", "motivo_descarte": "fora do escopo"}]
    concluir_retroalimentacao(projeto_em_revisao.id, armazem, novas=outra, licoes=LICOES[:1])

    processar_em_revisao(evento_em_revisao(projeto_em_revisao.id), armazem)

    assert [p.descricao for p in projetos.proximos_passos(projeto_em_revisao.id)] == ["Só este"]
    assert ideias_derivadas(banco) == []


def test_2_2_sem_tentativa_concluida_registra_erro_e_nao_levanta(
    projeto_em_revisao, armazem, caplog
):
    with caplog.at_level(logging.ERROR, logger="ais4.processos.retroalimentacao"):
        processar_em_revisao(evento_em_revisao(projeto_em_revisao.id), armazem)

    assert str(projeto_em_revisao.id) in caplog.text
    assert projetos.proximos_passos(projeto_em_revisao.id) == []
    with pytest.raises(RegraViolada):  # o gatilho do banco segura a conclusão até um humano agir
        projetos.transicionar(projeto_em_revisao.id, "concluido", "ana")


@pytest.mark.parametrize("estrago", ["json_invalido", "conteudo_adulterado", "artefato_ausente"])
def test_2_2_artefato_ilegivel_nao_derruba_o_outro(projeto_em_revisao, armazem, caplog, estrago):
    novas = {"json_invalido": b"{nao e json", "artefato_ausente": None}.get(estrago, NOVAS)
    t = concluir_retroalimentacao(projeto_em_revisao.id, armazem, novas=novas)
    if estrago == "conteudo_adulterado":
        uri = t.saidas["novas_ideias"]["uri"]
        Path(url2pathname(unquote(urlparse(uri).path))).write_bytes(b"[]")

    with caplog.at_level(logging.ERROR, logger="ais4.processos.retroalimentacao"):
        processar_em_revisao(evento_em_revisao(projeto_em_revisao.id), armazem)

    assert "novas_ideias" in caplog.text
    assert projetos.proximos_passos(projeto_em_revisao.id) == []
    assert len(projetos.licoes(projeto_em_revisao.id)) == len(LICOES)  # as lições seguem


def test_2_2_formato_invalido_registra_os_problemas(projeto_em_revisao, armazem, caplog):
    ruim = [{"proximo_passo": "x", "acao": "descartar"}, {"acao": "ideia"}]
    concluir_retroalimentacao(projeto_em_revisao.id, armazem, novas=ruim, licoes=[])

    with caplog.at_level(logging.ERROR, logger="ais4.processos.retroalimentacao"):
        processar_em_revisao(evento_em_revisao(projeto_em_revisao.id), armazem)

    assert "motivo_descarte" in caplog.text and "licoes_aprendidas" in caplog.text
    assert projetos.proximos_passos(projeto_em_revisao.id) == []
    assert projetos.licoes(projeto_em_revisao.id) == []


def test_2_2_acao_ideia_sem_objeto_usa_o_retroalimentar_do_playbook(projeto_em_revisao, armazem):
    novas = [{"proximo_passo": "Estender a série até 2060", "acao": "ideia"}]
    concluir_retroalimentacao(projeto_em_revisao.id, armazem, novas=novas)

    processar_em_revisao(evento_em_revisao(projeto_em_revisao.id), armazem)

    [passo] = projetos.proximos_passos(projeto_em_revisao.id)
    ideia = acervo.obter(passo.ideia_gerada_id)
    assert ideia.titulo == "Estender a série até 2060"
    assert (ideia.origem, ideia.status) == ("retroalimentacao", "rascunho")
    assert ideia.dominio == IDEIA_SEMENTE.dominio  # herdado da ideia de origem


def test_conclusao_apos_2_2_passa_trigger(limpar, armazem):
    acervo.transicionar(IDEIA_SEMENTE.id, "qualificada", "ana")
    projeto = novo_projeto()
    with eventos.isolado(vazio=True):
        acervo_handlers.registrar()
        eventos.publicar_pendentes("projetos")  # ProjetoCriado: ideia -> em_projeto
        assert acervo.obter(IDEIA_SEMENTE.id).status == "em_projeto"
        aprovar_todos_os_gates(projeto.id)
        ir_para_revisao(projeto.id)
        concluir_retroalimentacao(projeto.id, armazem)
        processar_em_revisao(evento_em_revisao(projeto.id), armazem)

        projetos.transicionar(projeto.id, "concluido", "ana")  # o gatilho do banco aceita

        assert projetos.obter(projeto.id).status == "concluido"
        eventos.publicar_pendentes("projetos")
    assert acervo.obter(IDEIA_SEMENTE.id).status == "concluida"


def test_registrar_liga_o_handler_ao_outbox_de_projetos(
    projeto_em_revisao, armazem, monkeypatch, banco
):
    assert "ais4.processos.retroalimentacao" in eventos.MODULOS_COM_HANDLERS
    monkeypatch.setenv("AIS4_ARTEFATOS_DIR", str(armazem.raiz))
    config.obter_config.cache_clear()
    try:
        concluir_retroalimentacao(projeto_em_revisao.id, armazem)
        with eventos.isolado(vazio=True):
            retroalimentacao.registrar()
            eventos.publicar_pendentes("projetos")
    finally:
        config.obter_config.cache_clear()

    assert len(projetos.proximos_passos(projeto_em_revisao.id)) == len(NOVAS)
    assert len(ideias_derivadas(banco)) == 2
