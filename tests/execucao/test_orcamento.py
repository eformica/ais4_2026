from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from pydantic import BaseModel

from ais4.comum.erros import RegraViolada
from ais4.execucao import orcamento
from ais4.execucao import servico as tarefas
from ais4.llm.cliente import OrcamentoExcedido
from ais4.llm.falso import ClienteLLMFalso
from tests.execucao.conftest import amanha, consultar, definir, ontem, uso
from tests.projetos.fabrica import novo_projeto


# --- definir / obter_ou_padrao -------------------------------------------------------------
def test_obter_ou_padrao_cria_com_os_padroes_da_config_e_sem_prazo(projeto_id, banco):
    assert consultar(banco, "select count(*) from execucao.orcamento")[0][0] == 0

    dto = orcamento.obter_ou_padrao(projeto_id, "ana")

    assert dto.projeto_id == projeto_id
    assert dto.max_tokens == 20_000_000
    assert dto.max_custo_usd == Decimal("50.00")
    assert dto.prazo is None
    assert consultar(banco, "select count(*) from execucao.orcamento")[0][0] == 1


def test_obter_ou_padrao_nao_sobrescreve_o_orcamento_existente(projeto_id):
    definir(projeto_id, tokens=500, usd="2.50")
    dto = orcamento.obter_ou_padrao(projeto_id, "ana")
    assert (dto.max_tokens, dto.max_custo_usd) == (500, Decimal("2.50"))


def test_definir_e_upsert(projeto_id):
    definir(projeto_id, tokens=500, usd="2.50", prazo=amanha())
    definir(projeto_id, tokens=900, usd="7.00", prazo=None)

    dto = orcamento.obter_ou_padrao(projeto_id, "ana")

    assert (dto.max_tokens, dto.max_custo_usd, dto.prazo) == (900, Decimal("7.00"), None)


def test_definir_le_prazo_sem_fuso_como_utc(projeto_id):
    orcamento.definir(projeto_id, 10, Decimal(1), datetime(2030, 5, 1, 12, 0), "ana")
    prazo = orcamento.obter_ou_padrao(projeto_id, "ana").prazo
    assert prazo == datetime(2030, 5, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(("tokens", "usd"), [(0, "1"), (-1, "1"), (10, "0"), (10, "-1")])
def test_definir_rejeita_limites_nao_positivos(projeto_id, tokens, usd):
    with pytest.raises(RegraViolada):
        orcamento.definir(projeto_id, tokens, Decimal(usd), None, "ana")


def test_definir_projeto_inexistente_e_regra_violada(banco, limpar):
    with pytest.raises(RegraViolada):
        orcamento.definir(uuid4(), 10, Decimal(1), None, "ana")


# --- registrar_chamada / consumo -----------------------------------------------------------
def test_registrar_chamada_grava_todos_os_campos(projeto_id, banco):
    orcamento.registrar_chamada(
        uso("0.123456", entrada=10, saida=20, cache=30, buscas=2, request_id="req_x"),
        projeto_id,
        None,
        None,
    )

    (linha,) = consultar(
        banco,
        "select projeto_id, tarefa_id, proposta_id, modelo, tokens_entrada, tokens_saida, "
        "tokens_cache_leitura, buscas_web, custo_usd::text, duracao_ms, request_id "
        "from execucao.chamada_llm",
    )
    esperado = (projeto_id, None, None, "claude-opus-5-5", 10, 20, 30, 2, "0.123456", 12, "req_x")
    assert linha == esperado


def test_chamada_de_proposta_nao_tem_projeto_e_nao_entra_no_consumo(projeto_id, banco):
    proposta = uuid4()
    orcamento.registrar_chamada(uso(), None, None, proposta)

    assert consultar(banco, "select projeto_id, proposta_id from execucao.chamada_llm") == [
        (None, proposta)
    ]
    assert consultar(banco, "select count(*) from execucao.consumo")[0][0] == 0


def test_consumo_soma_entrada_saida_e_cache_por_projeto(projeto_id):
    outro = novo_projeto(titulo="Outro").id
    orcamento.registrar_chamada(
        uso("0.30", entrada=100, saida=50, cache=25), projeto_id, None, None
    )
    orcamento.registrar_chamada(uso("0.20", entrada=10, saida=5, cache=0), projeto_id, None, None)
    orcamento.registrar_chamada(uso("9.00", entrada=1, saida=1), outro, None, None)

    gasto = orcamento.consumo(projeto_id)

    assert gasto.tokens == 100 + 50 + 25 + 10 + 5
    assert gasto.custo_usd == Decimal("0.50")
    assert orcamento.consumo(outro).custo_usd == Decimal("9.00")


def test_consumo_sem_chamadas_e_zero(projeto_id):
    gasto = orcamento.consumo(projeto_id)
    assert (gasto.tokens, gasto.custo_usd) == (0, Decimal(0))


def test_chamada_llm_e_somente_insercao(projeto_id, banco):
    orcamento.registrar_chamada(uso(), projeto_id, None, None)
    for comando in (
        "update execucao.chamada_llm set custo_usd = 0",
        "delete from execucao.chamada_llm",
    ):
        with psycopg.connect(banco) as c, pytest.raises(psycopg.errors.CheckViolation):
            c.execute(comando)


def test_chamada_llm_exige_projeto_ou_proposta(banco, limpar):
    # R21: uma chamada sem dono não entraria em orçamento nenhum
    with pytest.raises(RegraViolada):
        orcamento.registrar_chamada(uso(), None, None, None)
    assert consultar(banco, "select count(*) from execucao.chamada_llm")[0][0] == 0


def test_registrar_chamada_com_tarefa_grava_o_vinculo(projeto_id, banco):
    t = tarefas.criar_tarefa(projeto_id, "contexto", "pesquisador", "claude-opus-5-5", {}, 1)
    orcamento.registrar_chamada(uso(), projeto_id, t.id, None)
    assert consultar(banco, "select tarefa_id from execucao.chamada_llm") == [(t.id,)]


# --- verificar ------------------------------------------------------------------------------
def test_verificar_sem_orcamento_nem_consumo_passa_e_cria_o_padrao(projeto_id, banco):
    orcamento.verificar(projeto_id)
    assert consultar(banco, "select count(*) from execucao.orcamento")[0][0] == 1


def test_orcamento_custo_excedido_levanta(projeto_id):
    definir(projeto_id, usd="1.00")
    orcamento.registrar_chamada(uso("0.70"), projeto_id, None, None)
    orcamento.verificar(projeto_id)  # 0.70 < 1.00
    orcamento.registrar_chamada(uso("0.50"), projeto_id, None, None)

    with pytest.raises(OrcamentoExcedido) as erro:
        orcamento.verificar(projeto_id)

    assert erro.value.dimensao == "custo"
    assert erro.value.consumido == Decimal("1.20")
    assert erro.value.limite == Decimal("1.00")
    assert "custo" in str(erro.value) and "1.20" in str(erro.value) and "1.00" in str(erro.value)


def test_custo_igual_ao_limite_ja_estoura(projeto_id):
    definir(projeto_id, usd="1.00")
    orcamento.registrar_chamada(uso("1.00"), projeto_id, None, None)
    with pytest.raises(OrcamentoExcedido) as erro:
        orcamento.verificar(projeto_id)
    assert erro.value.dimensao == "custo"


def test_tokens_contam_entrada_saida_e_leitura_de_cache(projeto_id):
    definir(projeto_id, tokens=1000)
    orcamento.registrar_chamada(uso(entrada=400, saida=300, cache=200), projeto_id, None, None)
    orcamento.verificar(projeto_id)  # 900 < 1000
    orcamento.registrar_chamada(uso(entrada=50, saida=50, cache=0), projeto_id, None, None)

    with pytest.raises(OrcamentoExcedido) as erro:
        orcamento.verificar(projeto_id)

    assert (erro.value.dimensao, erro.value.consumido, erro.value.limite) == ("tokens", 1000, 1000)


def test_orcamento_prazo_vencido_levanta(projeto_id):
    prazo = ontem()
    definir(projeto_id, prazo=prazo)

    with pytest.raises(OrcamentoExcedido) as erro:
        orcamento.verificar(projeto_id)

    assert erro.value.dimensao == "prazo"
    assert erro.value.limite == prazo
    assert erro.value.consumido > prazo
    assert "prazo" in str(erro.value)


def test_prazo_futuro_ou_ausente_nao_levanta(projeto_id):
    definir(projeto_id, prazo=datetime.now(UTC) + timedelta(seconds=30))
    orcamento.verificar(projeto_id)
    definir(projeto_id, prazo=None)
    orcamento.verificar(projeto_id)


def test_orcamento_de_outro_projeto_nao_interfere(projeto_id):
    outro = novo_projeto(titulo="Outro").id
    definir(outro, usd="0.01")
    orcamento.registrar_chamada(uso("5.00"), outro, None, None)
    orcamento.verificar(projeto_id)  # o estouro é do outro projeto
    with pytest.raises(OrcamentoExcedido):
        orcamento.verificar(outro)


# --- vigiar ---------------------------------------------------------------------------------
def test_vigiar_dentro_do_orcamento_so_registra(projeto_id, banco):
    t = tarefas.criar_tarefa(projeto_id, "contexto", "pesquisador", "claude-opus-5-5", {}, 1)
    definir(projeto_id, usd="1.00")

    orcamento.vigiar(projeto_id, t.id)(uso("0.40"))

    assert orcamento.consumo(projeto_id).custo_usd == Decimal("0.40")
    assert consultar(banco, "select tarefa_id from execucao.chamada_llm") == [(t.id,)]


def test_vigiar_grava_a_chamada_mesmo_quando_a_verificacao_levanta(projeto_id, banco):
    definir(projeto_id, usd="1.00")
    ao_usar = orcamento.vigiar(projeto_id, None)

    with pytest.raises(OrcamentoExcedido):
        ao_usar(uso("1.20"))

    assert consultar(banco, "select custo_usd::text from execucao.chamada_llm") == [("1.200000",)]


def test_vigiar_interrompe_o_cliente_falso_depois_de_gravar(projeto_id, banco):
    class Saida(BaseModel):
        x: int

    definir(projeto_id, usd="0.01")
    orcamento.registrar_chamada(uso("0.009"), projeto_id, None, None)  # a falsa soma 0.0014
    cliente = ClienteLLMFalso({"p": [Saida(x=1)]})

    with pytest.raises(OrcamentoExcedido):
        cliente.estruturado(
            papel="p",
            sistema="s",
            mensagem="m",
            formato=Saida,
            ao_usar=orcamento.vigiar(projeto_id, None),
        )

    assert consultar(banco, "select count(*) from execucao.chamada_llm")[0][0] == 2


# --- OrcamentoExcedido ----------------------------------------------------------------------
def test_orcamento_excedido_continua_aceitando_so_uma_mensagem():
    e = OrcamentoExcedido("acabou")
    assert str(e) == "acabou"
    assert (e.dimensao, e.consumido, e.limite) == (None, None, None)
    assert str(OrcamentoExcedido()) == "orçamento excedido"


def test_orcamento_excedido_monta_mensagem_util_dos_campos():
    e = OrcamentoExcedido(dimensao="tokens", consumido=1200, limite=1000)
    assert str(e) == "orçamento de tokens excedido: consumido 1200, limite 1000"
