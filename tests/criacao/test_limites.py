"""Limites do Motor de Criação contra gasto sem fim (revisão final #1, #2 e #7; R31).

(a) teto de custo por proposta; (b) ideia que falha N vezes seguidas volta à triagem;
(c) erro transitório da API devolve a proposta sem contar como falha da ideia e o laço recua;
#7 proposta abandonada por um motor morto é devolvida e libera a ideia.
"""

import threading
from types import SimpleNamespace

import anthropic
import httpx
import psycopg
import pytest

from ais4 import config
from ais4.acervo import servico as acervo
from ais4.criacao import motor
from ais4.criacao.validacao import ResultadoCritica
from ais4.llm.cliente import RespostaInvalida
from ais4.llm.falso import ClienteLLMFalso
from tests.criacao.fabrica import (
    consultar,
    espec_completa,
    qualificar_semente_com_uma_fonte,
    verificacao_acessivel,
)
from tests.playbooks.semente import IDEIA_SEMENTE

REDATOR = "redator_projeto"
CRITICO = "critico_projeto"
VERIFICADOR = "verificador_fontes"
ESPEC = espec_completa(IDEIA_SEMENTE)
LIMPA = ResultadoCritica(lacunas=[])
IDEIA = IDEIA_SEMENTE.id


def _http(codigo: int) -> httpx.Response:
    return httpx.Response(codigo, request=httpx.Request("POST", "https://api.exemplo"))


def _sobrecarga():
    return anthropic.OverloadedError("sobrecarga", response=_http(529), body=None)


def _llm_redacao_falha() -> ClienteLLMFalso:
    """Um ciclo inteiro em que a redação falha nas 3 rodadas: a proposta termina devolvida."""
    return ClienteLLMFalso(
        {
            VERIFICADOR: [verificacao_acessivel()],
            REDATOR: [RespostaInvalida("max_tokens")] * 3,
        }
    )


def _llm_transitorio() -> ClienteLLMFalso:
    return ClienteLLMFalso({VERIFICADOR: [_sobrecarga()]})


def _llm_limpo() -> ClienteLLMFalso:
    return ClienteLLMFalso(
        {VERIFICADOR: [verificacao_acessivel()], REDATOR: [ESPEC], CRITICO: [LIMPA]}
    )


def _proposta(banco, proposta_id):
    (linha,) = consultar(
        banco, "select status, motivo from criacao.proposta where id = %s", (proposta_id,)
    )
    return linha


@pytest.fixture
def cfg(monkeypatch):
    """Altera valores da config (variáveis AIS4_*) só durante o teste."""

    def definir(**valores):
        for nome, valor in valores.items():
            monkeypatch.setenv(f"AIS4_{nome.upper()}", str(valor))
        config.obter_config.cache_clear()

    yield definir
    config.obter_config.cache_clear()


# --- (a) teto de custo por proposta ----------------------------------------------------------
def test_valores_provisorios_da_revisao_final():
    cfg = config.Config()
    assert str(cfg.orcamento_proposta_usd) == "5"
    assert cfg.max_devolucoes_ideia == 3
    assert cfg.max_tentativas_evento == 10
    assert cfg.proposta_abandonada_min == 120


def test_teto_por_proposta_devolve_a_proposta_e_para_de_chamar_o_modelo(banco, limpar, cfg):
    # cada uso do cliente falso custa 0.0014: verificação (2 usos) = 0.0028; o redator estoura
    cfg(orcamento_proposta_usd="0.004")
    qualificar_semente_com_uma_fonte()
    llm = ClienteLLMFalso(
        {VERIFICADOR: [verificacao_acessivel()], REDATOR: [ESPEC] * 3, CRITICO: [LIMPA] * 3}
    )

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    status, motivo = _proposta(banco, proposta_id)
    assert status == "devolvida"
    assert motivo.startswith("orçamento da proposta excedido: ")
    assert "0.004" in motivo
    assert [c.papel for c in llm.chamadas] == [VERIFICADOR, REDATOR]  # nada depois do estouro
    (gasto,) = consultar(
        banco,
        "select sum(custo_usd)::text from execucao.chamada_llm where proposta_id = %s",
        (proposta_id,),
    )
    assert gasto == ("0.004200",)  # a chamada que estourou foi registrada
    assert acervo.obter(IDEIA).status == "qualificada"


def test_teto_nao_vale_quando_ao_usar_e_informado(banco, limpar, cfg):
    cfg(orcamento_proposta_usd="0.001")
    qualificar_semente_com_uma_fonte()

    proposta_id = motor.executar_ciclo(_llm_limpo(), "motor-1", motor.registrar_uso_noop)

    assert _proposta(banco, proposta_id)[0] == "aguardando_aprovacao"


# --- (b) ideia que falha seguidas vezes volta à triagem ----------------------------------------
def test_tres_devolucoes_seguidas_devolvem_a_ideia_a_triagem(banco, limpar):
    qualificar_semente_com_uma_fonte()

    for _ in range(2):
        motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
        assert acervo.obter(IDEIA).status == "qualificada"
    ultima = motor.executar_ciclo(_llm_redacao_falha(), "motor-1")

    assert _proposta(banco, ultima) == ("devolvida", "falha na redação")
    ideia = acervo.obter(IDEIA)
    assert ideia.status == "triagem"
    assert ideia.motivo_status == "3 criações seguidas falharam: falha na redação"
    assert motor.executar_ciclo(_llm_limpo(), "motor-1") is None  # não é mais escolhida


def test_excecao_inesperada_tambem_conta_e_e_relancada(banco, limpar):
    qualificar_semente_com_uma_fonte()
    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
    llm = ClienteLLMFalso(
        {VERIFICADOR: [verificacao_acessivel()], REDATOR: [RuntimeError("falha de rede")]}
    )

    with pytest.raises(RuntimeError):
        motor.executar_ciclo(llm, "motor-1")

    ideia = acervo.obter(IDEIA)
    assert ideia.status == "triagem"
    assert ideia.motivo_status.startswith("3 criações seguidas falharam: RuntimeError")


def test_proposta_que_nao_falhou_interrompe_a_sequencia(banco, limpar):
    qualificar_semente_com_uma_fonte()
    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
    boa = motor.executar_ciclo(_llm_limpo(), "motor-1")
    # a proposta boa é rejeitada por um humano por outro caminho: simula com SQL
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute(
            "update criacao.proposta set status = 'rejeitada', motivo = 'x' where id = %s",
            (boa,),
        )

    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")

    assert acervo.obter(IDEIA).status == "qualificada"  # só 1 devolução desde a proposta boa


def test_devolucoes_transitorias_nao_contam(banco, limpar):
    qualificar_semente_com_uma_fonte()
    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
    for _ in range(4):
        motor.executar_ciclo(_llm_transitorio(), "motor-1")
    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
    assert acervo.obter(IDEIA).status == "qualificada"  # 2 falhas que contam, 4 transitórias

    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")

    assert acervo.obter(IDEIA).status == "triagem"


def test_requalificar_a_ideia_zera_a_contagem(banco, limpar):
    qualificar_semente_com_uma_fonte()
    for _ in range(3):
        motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
    assert acervo.obter(IDEIA).status == "triagem"

    acervo.transicionar(IDEIA, "qualificada", "carla")  # a curadora corrigiu a ideia
    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")

    assert acervo.obter(IDEIA).status == "qualificada"


# --- (c) erro transitório da API ----------------------------------------------------------------
def test_erro_transitorio_devolve_com_prefixo_sem_gravar_fonte_e_sem_relancar(banco, limpar):
    qualificar_semente_com_uma_fonte()

    proposta_id = motor.executar_ciclo(_llm_transitorio(), "motor-1")

    status, motivo = _proposta(banco, proposta_id)
    assert status == "devolvida"
    assert motivo.startswith("api_transitoria: OverloadedError")
    assert consultar(banco, "select count(*) from criacao.fonte_verificada") == [(0,)]
    assert acervo.obter(IDEIA).status == "qualificada"
    assert motor.selecionar_e_reservar("motor-2") is not None  # a ideia não ficou presa


def test_erro_transitorio_na_redacao_tambem_e_transitorio(banco, limpar):
    qualificar_semente_com_uma_fonte()
    erro = anthropic.RateLimitError("devagar", response=_http(429), body=None)
    llm = ClienteLLMFalso({VERIFICADOR: [verificacao_acessivel()], REDATOR: [erro]})

    proposta_id = motor.executar_ciclo(llm, "motor-1")

    assert _proposta(banco, proposta_id)[1].startswith("api_transitoria: RateLimitError")


# --- (c) backoff do laço ------------------------------------------------------------------------
def _laco(monkeypatch, resultados: list[str | None], intervalo: float = 30.0) -> list[float]:
    """Roda `loop` com ciclos falsos: cada item é o motivo da devolução (None: sem trabalho)."""
    parar = threading.Event()
    esperas: list[float] = []
    fila = list(resultados)

    def ciclo_falso(cliente, usuario):
        motivo = fila.pop(0)
        if not fila:
            parar.set()
        return None if motivo is None else motivo

    def obter_falso(proposta_id):
        return SimpleNamespace(status="devolvida", motivo=proposta_id)

    monkeypatch.setattr(motor, "executar_ciclo", ciclo_falso)
    monkeypatch.setattr(motor, "obter_proposta", obter_falso)
    motor.loop("cliente", intervalo_s=intervalo, parar=parar, esperar=esperas.append)
    return esperas


def test_loop_dobra_a_espera_depois_de_erro_transitorio_e_volta_ao_normal(monkeypatch):
    t = "api_transitoria: OverloadedError: sobrecarga"
    esperas = _laco(monkeypatch, [t, t, t, "falha na redação", None, t])
    assert esperas == [60.0, 120.0, 240.0, 30.0, 30.0, 60.0]


def test_backoff_tem_teto_de_30_minutos(monkeypatch):
    t = "api_transitoria: RateLimitError"
    esperas = _laco(monkeypatch, [t] * 8, intervalo=60.0)
    assert esperas == [120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0, 1800.0, 1800.0]
    assert motor.BACKOFF_MAX_S == 1800


# --- #7 proposta abandonada ---------------------------------------------------------------------
def _envelhecer(banco, proposta_id, minutos: int) -> None:
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute("alter table criacao.proposta disable trigger proposta_atualizado_em")
        try:
            c.execute(
                "update criacao.proposta set atualizado_em = now() - make_interval(mins => %s) "
                "where id = %s",
                (minutos, proposta_id),
            )
        finally:
            c.execute("alter table criacao.proposta enable trigger proposta_atualizado_em")


@pytest.mark.parametrize("status", ["rascunho", "em_autocritica"])
def test_proposta_abandonada_e_devolvida_e_libera_a_ideia(banco, limpar, status):
    qualificar_semente_com_uma_fonte()
    _, orfa = motor.selecionar_e_reservar("motor-morto")  # o motor morreu (SIGKILL) aqui
    if status == "em_autocritica":
        with psycopg.connect(banco, autocommit=True) as c:
            c.execute(
                "update criacao.proposta set status = 'em_autocritica', "
                "playbook_id = '00000000-0000-4000-8000-000000000001', playbook_versao = '0.1.0' "
                "where id = %s",
                (orfa,),
            )
    _envelhecer(banco, orfa, 121)

    nova = motor.executar_ciclo(_llm_limpo(), "motor-1")

    assert _proposta(banco, orfa) == ("devolvida", "proposta abandonada (sem atividade há 121 min)")
    assert nova != orfa
    assert _proposta(banco, nova)[0] == "aguardando_aprovacao"


def test_proposta_ativa_recente_nao_e_devolvida(banco, limpar):
    qualificar_semente_com_uma_fonte()
    _, ativa = motor.selecionar_e_reservar("motor-2")
    _envelhecer(banco, ativa, 119)

    assert motor.executar_ciclo(_llm_limpo(), "motor-1") is None  # a ideia segue reservada

    assert _proposta(banco, ativa) == ("rascunho", None)


def test_ajustes_solicitados_nao_e_tratada_como_abandonada(banco, limpar):
    qualificar_semente_com_uma_fonte()
    proposta_id = motor.executar_ciclo(_llm_limpo(), "motor-1")
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute(
            "update criacao.proposta set status = 'ajustes_solicitados' where id = %s",
            (proposta_id,),
        )
    _envelhecer(banco, proposta_id, 600)

    motor._devolver_abandonadas("motor-1")

    assert _proposta(banco, proposta_id)[0] == "ajustes_solicitados"


def test_cada_fonte_verificada_renova_a_atividade_da_proposta(banco, limpar):
    qualificar_semente_com_uma_fonte()
    ideia, proposta_id = motor.selecionar_e_reservar("motor-1")
    _envelhecer(banco, proposta_id, 500)

    motor.verificar_fontes(
        proposta_id, ideia, ClienteLLMFalso({VERIFICADOR: [verificacao_acessivel()]})
    )

    (recente,) = consultar(
        banco,
        "select atualizado_em > now() - interval '1 minute' from criacao.proposta where id = %s",
        (proposta_id,),
    )
    assert recente == (True,)


def test_abandonadas_contam_como_falhas_da_ideia(banco, limpar):
    qualificar_semente_com_uma_fonte()
    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
    motor.executar_ciclo(_llm_redacao_falha(), "motor-1")
    _, orfa = motor.selecionar_e_reservar("motor-morto")
    _envelhecer(banco, orfa, 200)

    assert motor.executar_ciclo(_llm_limpo(), "motor-1") is None

    ideia = acervo.obter(IDEIA)
    assert ideia.status == "triagem"
    assert ideia.motivo_status.startswith("3 criações seguidas falharam: proposta abandonada")
