import json
import threading
from uuid import UUID, uuid4, uuid5

import pytest

from ais4.auth import Usuario
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.comum.espec import Escopo, EspecProjeto
from ais4.criacao import agentes, motor
from ais4.criacao.motor import DecisaoProposta
from ais4.criacao.validacao import Lacuna, ResultadoCritica
from ais4.llm.cliente import RespostaInvalida
from ais4.llm.falso import ClienteLLMFalso
from ais4.playbooks import servico as playbooks
from ais4.projetos import servico as projetos
from ais4.projetos.servico import CriarProjeto
from tests.criacao.fabrica import (
    CENSO,
    URL_CENSO,
    consultar,
    espec_completa,
    qualificar_semente_com_uma_fonte,
    verificacao_acessivel,
)
from tests.playbooks.semente import IDEIA_SEMENTE
from tests.projetos.fabrica import RESPONSAVEIS, usuario

REDATOR = "redator_projeto"
CRITICO = "critico_projeto"
VERIFICADOR = "verificador_fontes"
CUSTO_CHAMADA = "0.001400"  # o Uso padrão do ClienteLLMFalso

ESPEC = espec_completa(IDEIA_SEMENTE)
LIMPA = ResultadoCritica(lacunas=[])
APONTA = ResultadoCritica(lacunas=[Lacuna(secao="riscos", mensagem="mitigação implausível")])
DONO = usuario("ana", "dono_do_projeto")
GUARDIAO = Usuario("fred", frozenset({"guardiao_de_playbooks"}))
SEMENTE_PLAYBOOK_ID = UUID("00000000-0000-4000-8000-000000000001")
RESP_GATES = {k: v for k, v in RESPONSAVEIS.items() if k != "dono_do_projeto"}


def _llm(redator, critico, verificacoes=1):
    roteiro = {REDATOR: list(redator), CRITICO: list(critico)}
    if verificacoes:
        roteiro[VERIFICADOR] = [verificacao_acessivel() for _ in range(verificacoes)]
    return ClienteLLMFalso(roteiro)


def _proposta(banco, proposta_id):
    (linha,) = consultar(
        banco,
        "select status, iteracoes, motivo, conteudo, projeto_id, decidido_por, playbook_id, "
        "playbook_versao, solicitado_por from criacao.proposta where id = %s",
        (proposta_id,),
    )
    return dict(
        zip(
            (
                "status",
                "iteracoes",
                "motivo",
                "conteudo",
                "projeto_id",
                "decidido_por",
                "playbook_id",
                "playbook_versao",
                "solicitado_por",
            ),
            linha,
            strict=True,
        )
    )


def _iteracoes(banco, proposta_id):
    return consultar(
        banco,
        "select n, rascunho, lacunas, aprovada_pela_critica, custo_usd::text "
        "from criacao.proposta_iteracao where proposta_id = %s order by n",
        (proposta_id,),
    )


def _status_da_ideia(banco):
    return consultar(banco, "select status from acervo.ideia where id = %s", (IDEIA_SEMENTE.id,))[
        0
    ][0]


def _ciclo_aguardando(banco, solicitante="motor-1"):
    """Um ciclo limpo: a proposta termina em `aguardando_aprovacao`."""
    qualificar_semente_com_uma_fonte()
    proposta_id = motor.executar_ciclo(_llm([ESPEC], [LIMPA]), solicitante)
    assert _proposta(banco, proposta_id)["status"] == "aguardando_aprovacao"
    return proposta_id


# --- ciclo: rascunho e autocrítica ------------------------------------------------------------
def test_ciclo_limpo_termina_em_aguardando_aprovacao(banco, limpar):
    qualificar_semente_com_uma_fonte()
    llm = _llm([ESPEC], [LIMPA])

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    p = _proposta(banco, proposta_id)
    assert p["status"] == "aguardando_aprovacao"
    assert p["iteracoes"] == 1
    assert p["solicitado_por"] == "motor-1"
    assert p["playbook_versao"] == "0.1.0" and p["playbook_id"] is not None
    assert EspecProjeto.model_validate(p["conteudo"]).objetivos == ESPEC.objetivos
    ((n, rascunho, lacunas, aprovada, custo),) = _iteracoes(banco, proposta_id)
    assert (n, lacunas, aprovada) == (1, [], True)
    assert rascunho == p["conteudo"]
    assert custo == "0.002800"  # redator + crítico: o Uso reportado durante a rodada
    # a ideia continua qualificada até o gate: só o evento ProjetoCriado a move
    assert _status_da_ideia(banco) == "qualificada"


def test_ciclo_registra_as_chamadas_llm_com_o_proposta_id(banco, limpar):
    qualificar_semente_com_uma_fonte()

    proposta_id = motor.executar_ciclo(_llm([ESPEC], [LIMPA]), "motor-1")

    linhas = consultar(
        banco,
        "select proposta_id, projeto_id, tarefa_id, modelo, custo_usd::text "
        "from execucao.chamada_llm order by id",
    )
    # verificador (1 ferramenta + resposta final), redator e crítico: 4 usos de 0.0014
    assert len(linhas) == 4
    assert {(l[0], l[1], l[2], l[3], l[4]) for l in linhas} == {
        (proposta_id, None, None, "claude-opus-5-5", CUSTO_CHAMADA)
    }


def test_ao_usar_informado_substitui_o_registro_padrao(banco, limpar):
    qualificar_semente_com_uma_fonte()
    usos = []

    motor.executar_ciclo(_llm([ESPEC], [LIMPA]), "motor-1", usos.append)

    assert len(usos) == 4
    assert consultar(banco, "select count(*) from execucao.chamada_llm")[0][0] == 0


def test_redigir_e_autocriticar_isolados_registram_na_proposta(banco, limpar):
    qualificar_semente_com_uma_fonte()
    _, proposta_id = motor.selecionar_e_reservar("motor-1")
    ideia = IDEIA_SEMENTE
    casado = motor.casar_playbook(ideia)
    motor.vincular_playbook(proposta_id, casado[0], "motor-1")
    llm = ClienteLLMFalso({REDATOR: [ESPEC], CRITICO: [LIMPA]})

    espec = motor.redigir(proposta_id, llm, [])
    motor.autocriticar(proposta_id, espec, playbooks.etapas(SEMENTE_PLAYBOOK_ID, "0.1.0"), llm)

    assert consultar(banco, "select proposta_id from execucao.chamada_llm") == [
        (proposta_id,),
        (proposta_id,),
    ]


def test_motor_sobrescreve_metodologia_e_fontes_do_redator(banco, limpar):
    qualificar_semente_com_uma_fonte()
    inventado = ESPEC.model_copy(
        update={
            "metodologia": ESPEC.metodologia.model_copy(
                update={"playbook_codigo": "outro", "playbook_versao": "9.9.9"}
            ),
            "fontes_dados": [
                ESPEC.fontes_dados[0].model_copy(
                    update={
                        "nome": "Inventada",
                        "url": "https://x.test",
                        "status_acesso": "acessivel",
                    }
                )
            ],
        }
    )

    proposta_id = motor.executar_ciclo(_llm([inventado], [LIMPA]), "motor-1")

    conteudo = EspecProjeto.model_validate(_proposta(banco, proposta_id)["conteudo"])
    assert (conteudo.metodologia.playbook_codigo, conteudo.metodologia.playbook_versao) == (
        "projeto-academico-simples",
        "0.1.0",
    )
    (fonte,) = conteudo.fontes_dados
    assert (fonte.nome, fonte.url, fonte.status_acesso) == (CENSO, URL_CENSO, "acessivel")
    assert "2022" in fonte.cobertura and fonte.licenca == "CC BY 4.0"


def test_redator_recebe_o_esqueleto_as_fontes_verificadas_e_o_feedback(banco, limpar):
    qualificar_semente_com_uma_fonte()
    llm = _llm([ESPEC, ESPEC], [APONTA, LIMPA])

    motor.executar_ciclo(llm, "motor-1")

    redacoes = [c for c in llm.chamadas if c.papel == REDATOR]
    criticas = [c for c in llm.chamadas if c.papel == CRITICO]
    assert len(redacoes) == 2 and len(criticas) == 2
    assert redacoes[0].formato is EspecProjeto
    assert redacoes[0].sistema == agentes.PROMPT_REDATOR_PROJETO
    assert criticas[0].sistema == agentes.PROMPT_CRITICO_PROJETO
    assert URL_CENSO in redacoes[0].mensagem and "projeto-academico-simples" in redacoes[0].mensagem
    assert "mitigação implausível" not in redacoes[0].mensagem
    # 2ª rodada: o feedback da crítica e o rascunho anterior
    assert "riscos: mitigação implausível" in redacoes[1].mensagem
    assert "Risco de dados" in redacoes[1].mensagem
    assert "Objetivo" in criticas[0].mensagem


def test_autocritica_para_em_3_iteracoes(banco, limpar):
    qualificar_semente_com_uma_fonte()
    llm = _llm([ESPEC] * 3, [APONTA] * 3)  # um 4º uso do roteiro estoura RoteiroEsgotado

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    p = _proposta(banco, proposta_id)
    assert (p["status"], p["iteracoes"]) == ("aguardando_aprovacao", 3)
    linhas = _iteracoes(banco, proposta_id)
    assert [(n, aprovada) for n, _, _, aprovada, _ in linhas] == [
        (1, False),
        (2, False),
        (3, False),
    ]
    # as lacunas restantes ficam visíveis para o revisor
    assert linhas[-1][2] == [{"secao": "riscos", "mensagem": "mitigação implausível"}]


def test_autocritica_para_quando_sem_lacunas(banco, limpar):
    qualificar_semente_com_uma_fonte()
    llm = _llm([ESPEC, ESPEC], [APONTA, LIMPA])

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    assert _proposta(banco, proposta_id)["iteracoes"] == 2
    linhas = _iteracoes(banco, proposta_id)
    assert [aprovada for _, _, _, aprovada, _ in linhas] == [False, True]
    assert linhas[1][2] == []


def test_lacunas_do_validador_deterministico_entram_na_autocritica(banco, limpar):
    qualificar_semente_com_uma_fonte()
    sem_fora = ESPEC.model_copy(update={"escopo": Escopo(dentro=["x"])})
    llm = _llm([sem_fora, ESPEC], [LIMPA, LIMPA])

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    linhas = _iteracoes(banco, proposta_id)
    assert [lacuna["secao"] for lacuna in linhas[0][2]] == ["escopo"]
    assert linhas[0][3] is False and linhas[1][3] is True
    # o crítico LLM também recebe o que o validador já achou
    criticas = [c for c in llm.chamadas if c.papel == CRITICO]
    assert "escopo" in criticas[0].mensagem


def test_status_durante_as_rodadas_e_em_autocritica(banco, limpar):
    qualificar_semente_com_uma_fonte()
    vistos = []

    llm = _llm([ESPEC], [LIMPA])
    original = llm.estruturado

    def estruturado(**kw):
        if kw["papel"] == REDATOR:
            vistos.append(consultar(banco, "select status from criacao.proposta")[0][0])
        return original(**kw)

    llm.estruturado = estruturado
    motor.executar_ciclo(llm, "motor-1")
    assert vistos == ["em_autocritica"]


def test_redator_com_resposta_invalida_vira_lacuna_e_a_rodada_seguinte_segue(banco, limpar):
    qualificar_semente_com_uma_fonte()
    llm = _llm([RespostaInvalida("schema", "campo x"), ESPEC], [LIMPA])

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    p = _proposta(banco, proposta_id)
    assert (p["status"], p["iteracoes"]) == ("aguardando_aprovacao", 2)
    linhas = _iteracoes(banco, proposta_id)
    assert linhas[0][2] == [{"secao": "sistema", "mensagem": "redação falhou: schema"}]
    assert linhas[0][3] is False
    assert linhas[0][4] == CUSTO_CHAMADA  # a chamada recusada foi cobrada
    assert linhas[1][3] is True
    assert len([c for c in llm.chamadas if c.papel == CRITICO]) == 1  # sem rascunho, sem crítica


def test_sem_nenhum_rascunho_valido_devolve_a_proposta_e_mantem_a_ideia(banco, limpar):
    qualificar_semente_com_uma_fonte()
    llm = _llm([RespostaInvalida("max_tokens")] * 3, [])

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    p = _proposta(banco, proposta_id)
    assert (p["status"], p["motivo"], p["conteudo"]) == ("devolvida", "falha na redação", None)
    assert _status_da_ideia(banco) == "qualificada"
    # a ideia será tentada de novo no próximo ciclo, com uma nova proposta
    novo = motor.executar_ciclo(_llm([ESPEC], [LIMPA]), "motor-1")
    assert novo != proposta_id
    assert _proposta(banco, novo)["status"] == "aguardando_aprovacao"


def test_critico_com_resposta_invalida_vira_lacuna_e_segue_com_as_deterministicas(banco, limpar):
    qualificar_semente_com_uma_fonte()
    llm = _llm([ESPEC, ESPEC], [RespostaInvalida("refusal"), LIMPA])

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    linhas = _iteracoes(banco, proposta_id)
    assert linhas[0][2] == [{"secao": "sistema", "mensagem": "crítica indisponível: refusal"}]
    assert linhas[0][3] is False and linhas[1][3] is True


def test_excecao_inesperada_devolve_a_proposta_e_repropaga(banco, limpar):
    qualificar_semente_com_uma_fonte()
    llm = _llm([RuntimeError("falha de rede")], [])

    with pytest.raises(RuntimeError, match="falha de rede"):
        motor.executar_ciclo(llm, "motor-1")

    (linha,) = consultar(banco, "select status, motivo from criacao.proposta")
    assert linha[0] == "devolvida" and "RuntimeError: falha de rede" in linha[1]
    assert _status_da_ideia(banco) == "qualificada"
    # nenhuma proposta ativa bloqueia a ideia
    assert motor.selecionar_e_reservar("motor-2") is not None


def test_sem_playbook_aplicavel_devolve_a_ideia_para_a_triagem(banco, limpar):
    qualificar_semente_com_uma_fonte()
    playbooks.alterar_status(SEMENTE_PLAYBOOK_ID, "0.1.0", "depreciado", GUARDIAO)
    llm = ClienteLLMFalso({})

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    assert _proposta(banco, proposta_id)["status"] == "devolvida"
    assert _status_da_ideia(banco) == "triagem"
    assert llm.chamadas == []


def test_sem_ideia_qualificada_devolve_none(banco, limpar):
    assert motor.executar_ciclo(ClienteLLMFalso({}), "motor-1") is None


# --- gate: aprovar, ajustar, rejeitar ---------------------------------------------------------
def test_aprovar_cria_projeto(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)

    dto = motor.decidir(proposta_id, DecisaoProposta(acao="aprovar", responsaveis=RESP_GATES), DONO)

    assert dto.status == "aprovada" and dto.projeto_id is not None
    p = _proposta(banco, proposta_id)
    assert (p["status"], p["decidido_por"], p["projeto_id"]) == ("aprovada", "ana", dto.projeto_id)
    ((titulo, ideia, dono, status, pb_versao),) = consultar(
        banco, "select titulo, ideia_origem_id, dono, status, playbook_versao from projetos.projeto"
    )
    assert (titulo, ideia, dono, status, pb_versao) == (
        IDEIA_SEMENTE.titulo,
        IDEIA_SEMENTE.id,
        "ana",
        "aprovado",
        "0.1.0",
    )
    ((versao, aprovado_por, conteudo),) = consultar(
        banco, "select versao, aprovado_por, conteudo from projetos.especificacao"
    )
    assert (versao, aprovado_por, conteudo) == (1, "ana", p["conteudo"])
    marcos = consultar(banco, "select codigo from projetos.marco order by ordem")
    assert [m[0] for m in marcos] == ["M1", "M2", "M3", "M4", "M5", "M6"]
    resp = dict(consultar(banco, "select papel, pessoa from projetos.responsavel"))
    assert resp == {**RESP_GATES, "dono_do_projeto": "ana"}
    assert consultar(banco, "select tipo_evento from projetos.outbox") == [("ProjetoCriado",)]


def test_aprovar_move_a_ideia_e_ela_nao_e_selecionada_de_novo_sem_o_outbox(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)

    motor.decidir(proposta_id, DecisaoProposta(acao="aprovar", responsaveis=RESP_GATES), DONO)

    # o outbox NÃO foi publicado: a ideia já saiu da fila por conta da própria aprovação
    assert _status_da_ideia(banco) == "em_projeto"
    assert motor.executar_ciclo(ClienteLLMFalso({}), "motor-1") is None
    assert consultar(banco, "select count(*) from projetos.projeto") == [(1,)]
    assert consultar(banco, "select count(*) from criacao.proposta") == [(1,)]


def test_aprovar_depois_do_projeto_ja_criado_e_idempotente(banco, limpar):
    """Queda entre `criar_projeto` e o commit em `criacao`: repetir a aprovação não duplica."""
    proposta_id = _ciclo_aguardando(banco)
    proposta = motor.obter_proposta(proposta_id)
    projeto_id = uuid5(motor.NAMESPACE_PROJETOS, str(proposta_id))
    projetos.criar_projeto(
        CriarProjeto(
            id=projeto_id,
            titulo=IDEIA_SEMENTE.titulo,
            ideia_origem_id=proposta.ideia_id,
            playbook_id=proposta.playbook_id,
            playbook_versao=proposta.playbook_versao,
            dono="ana",
            especificacao=EspecProjeto.model_validate(proposta.conteudo),
            aprovado_por="ana",
            responsaveis=RESP_GATES,
        ),
        "ana",
    )

    dto = motor.decidir(proposta_id, DecisaoProposta(acao="aprovar", responsaveis=RESP_GATES), DONO)

    assert dto.status == "aprovada" and dto.projeto_id == projeto_id
    assert consultar(banco, "select count(*) from projetos.projeto") == [(1,)]
    assert consultar(banco, "select count(*) from projetos.outbox") == [(1,)]
    assert _status_da_ideia(banco) == "em_projeto"


def test_excecao_baseexception_tambem_devolve_a_proposta(banco, limpar):
    qualificar_semente_com_uma_fonte()
    llm = _llm([KeyboardInterrupt()], [])

    with pytest.raises(KeyboardInterrupt):
        motor.executar_ciclo(llm, "motor-1")

    (linha,) = consultar(banco, "select status, motivo from criacao.proposta")
    assert linha[0] == "devolvida" and "KeyboardInterrupt" in linha[1]
    assert motor.selecionar_e_reservar("motor-2") is not None  # a ideia não ficou presa


def test_quem_solicitou_nao_aprova(banco, limpar):
    proposta_id = _ciclo_aguardando(banco, solicitante="ana")

    with pytest.raises(Proibido):
        motor.decidir(proposta_id, DecisaoProposta(acao="aprovar", responsaveis=RESP_GATES), DONO)

    assert _proposta(banco, proposta_id)["status"] == "aguardando_aprovacao"
    assert consultar(banco, "select count(*) from projetos.projeto") == [(0,)]


@pytest.mark.parametrize("acao", ["aprovar", "ajustar", "rejeitar"])
def test_so_o_dono_do_projeto_decide(banco, limpar, acao):
    proposta_id = _ciclo_aguardando(banco)
    rita = usuario("rita", "revisor_metodologico")

    with pytest.raises(Proibido):
        motor.decidir(proposta_id, DecisaoProposta(acao=acao, motivo="m", ajustes=["a"]), rita)


def test_decidir_exige_proposta_aguardando_aprovacao(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)
    motor.decidir(proposta_id, DecisaoProposta(acao="rejeitar", motivo="fora de foco"), DONO)

    with pytest.raises(RegraViolada, match="aguardando_aprovacao"):
        motor.decidir(proposta_id, DecisaoProposta(acao="aprovar", responsaveis=RESP_GATES), DONO)


def test_aprovar_sem_responsavel_de_gate_e_regra_violada_e_nada_muda(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)
    faltando = {k: v for k, v in RESP_GATES.items() if k != "curador_do_acervo"}

    with pytest.raises(RegraViolada, match="curador_do_acervo"):
        motor.decidir(proposta_id, DecisaoProposta(acao="aprovar", responsaveis=faltando), DONO)

    assert _proposta(banco, proposta_id)["status"] == "aguardando_aprovacao"
    assert consultar(banco, "select count(*) from projetos.projeto") == [(0,)]


def test_aprovar_com_papel_desconhecido_e_regra_violada(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)

    with pytest.raises(RegraViolada, match="papéis desconhecidos"):
        motor.decidir(
            proposta_id,
            DecisaoProposta(acao="aprovar", responsaveis={**RESP_GATES, "rei": "x"}),
            DONO,
        )


def test_rejeitar_sem_motivo_e_regra_violada(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)

    for motivo in (None, "   "):
        with pytest.raises(RegraViolada, match="motivo"):
            motor.decidir(proposta_id, DecisaoProposta(acao="rejeitar", motivo=motivo), DONO)

    assert _proposta(banco, proposta_id)["status"] == "aguardando_aprovacao"


def test_rejeitar_devolve_ideia_para_triagem(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)

    dto = motor.decidir(
        proposta_id, DecisaoProposta(acao="rejeitar", motivo="Escopo amplo demais"), DONO
    )

    assert dto.status == "rejeitada"
    p = _proposta(banco, proposta_id)
    assert (p["status"], p["motivo"], p["decidido_por"]) == (
        "rejeitada",
        "Escopo amplo demais",
        "ana",
    )
    assert _status_da_ideia(banco) == "triagem"
    (motivo,) = consultar(
        banco, "select motivo_status from acervo.ideia where id = %s", (IDEIA_SEMENTE.id,)
    )[0]
    assert "Escopo amplo demais" in motivo
    assert consultar(banco, "select count(*) from projetos.projeto") == [(0,)]


def test_ajustar_sem_ajustes_e_regra_violada(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)

    for ajustes in ([], ["  "]):
        with pytest.raises(RegraViolada, match="ajustes"):
            motor.decidir(proposta_id, DecisaoProposta(acao="ajustar", ajustes=ajustes), DONO)


def test_ajustar_registra_iteracao_humana_e_o_proximo_ciclo_retoma(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)
    dto = motor.decidir(
        proposta_id,
        DecisaoProposta(acao="ajustar", ajustes=["Incluir a PAM no escopo", "Reduzir o prazo"]),
        DONO,
    )
    assert dto.status == "ajustes_solicitados"
    p = _proposta(banco, proposta_id)
    assert (p["status"], p["iteracoes"]) == ("ajustes_solicitados", 2)
    humana = _iteracoes(banco, proposta_id)[1]
    assert humana[0] == 2 and humana[1] == p["conteudo"] and humana[3] is False
    assert humana[2] == [
        {"secao": "revisao_humana", "mensagem": "Incluir a PAM no escopo"},
        {"secao": "revisao_humana", "mensagem": "Reduzir o prazo"},
    ]
    assert humana[4] == "0.000000"

    # sem roteiro do verificador: as fontes já verificadas não são testadas de novo
    llm = ClienteLLMFalso({REDATOR: [ESPEC], CRITICO: [LIMPA]})
    retomada = motor.executar_ciclo(llm, "motor-1")

    assert retomada == proposta_id
    p = _proposta(banco, proposta_id)
    assert (p["status"], p["iteracoes"]) == ("aguardando_aprovacao", 3)
    redacao = next(c for c in llm.chamadas if c.papel == REDATOR)
    assert "revisao_humana: Incluir a PAM no escopo" in redacao.mensagem
    assert "revisao_humana: Reduzir o prazo" in redacao.mensagem
    assert p["solicitado_por"] == "motor-1"
    assert consultar(banco, "select count(*) from criacao.proposta") == [(1,)]


def test_retomada_tem_prioridade_sobre_nova_ideia_e_respeita_o_limite_por_chamada(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)
    motor.decidir(proposta_id, DecisaoProposta(acao="ajustar", ajustes=["x"]), DONO)
    llm = ClienteLLMFalso({REDATOR: [ESPEC] * 3, CRITICO: [APONTA] * 3})

    assert motor.executar_ciclo(llm, "motor-1") == proposta_id

    # 1 (ciclo original) + 1 (humana) + 3 rodadas desta chamada
    assert _proposta(banco, proposta_id)["iteracoes"] == 5


def test_decidir_proposta_inexistente_e_nao_encontrado(banco, limpar):
    with pytest.raises(NaoEncontrado):
        motor.decidir(uuid4(), DecisaoProposta(acao="rejeitar", motivo="m"), DONO)


# --- leitura ----------------------------------------------------------------------------------
def test_listar_obter_iteracoes_e_fontes(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)

    (listada,) = motor.listar_propostas("aguardando_aprovacao")
    assert listada.id == proposta_id and listada.ideia_id == IDEIA_SEMENTE.id
    assert motor.listar_propostas("aprovada") == []
    assert len(motor.listar_propostas()) == 1
    dto = motor.obter_proposta(proposta_id)
    assert dto.status == "aguardando_aprovacao" and dto.conteudo is not None
    (iteracao,) = motor.iteracoes_da_proposta(proposta_id)
    assert iteracao.n == 1 and iteracao.aprovada_pela_critica is True
    (fonte,) = motor.fontes_da_proposta(proposta_id)
    assert (fonte.nome, fonte.status, fonte.url) == (CENSO, "acessivel", URL_CENSO)
    assert json.dumps(iteracao.lacunas) == "[]"


# --- laço do worker ---------------------------------------------------------------------------
def test_loop_registra_o_erro_do_ciclo_e_continua(monkeypatch, caplog):
    parar = threading.Event()
    chamadas = []

    def ciclo_falso(cliente, usuario):
        chamadas.append((cliente, usuario))
        if len(chamadas) == 1:
            raise RuntimeError("falha de rede")
        parar.set()

    monkeypatch.setattr(motor, "executar_ciclo", ciclo_falso)

    motor.loop("cliente", intervalo_s=0, parar=parar)

    assert chamadas == [("cliente", "sistema:motor-criacao")] * 2
    assert "ciclo do motor de criação falhou" in caplog.text


def test_duas_aprovacoes_simultaneas_criam_um_so_projeto(banco, limpar):
    proposta_id = _ciclo_aguardando(banco)
    bruno = usuario("bruno", "dono_do_projeto")
    resultados = []

    def aprovar(quem):
        try:
            resultados.append(
                motor.decidir(
                    proposta_id, DecisaoProposta(acao="aprovar", responsaveis=RESP_GATES), quem
                )
            )
        except RegraViolada as erro:
            resultados.append(erro)

    threads = [threading.Thread(target=aprovar, args=(u,)) for u in (DONO, bruno)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(type(r).__name__ for r in resultados) == ["PropostaDTO", "RegraViolada"]
    assert consultar(banco, "select count(*) from projetos.projeto") == [(1,)]
