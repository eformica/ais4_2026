import pytest

from ais4.comum.erros import RegraViolada
from ais4.execucao.dag import estado_por_etapa, etapas_prontas, validar_dag
from ais4.playbooks.base import Etapa, PapelAgente
from tests.execucao.fabrica import ETAPAS_SEMENTE, marco, tarefa

POR_CODIGO = {e.codigo: e for e in ETAPAS_SEMENTE}


def _pendentes():
    return {e.codigo: "pendente" for e in ETAPAS_SEMENTE}


def test_a_semente_tem_um_gate_em_coleta_dados():
    assert POR_CODIGO["coleta_dados"].exige_gate
    assert not POR_CODIGO["contexto"].exige_gate


def test_etapas_prontas_semente():
    assert etapas_prontas(ETAPAS_SEMENTE, _pendentes()) == ["contexto"]


def test_concluir_sem_gate_libera_a_dependente():
    estado = estado_por_etapa(ETAPAS_SEMENTE, [tarefa("contexto", 1, "concluida")], [])
    assert estado["contexto"] == "concluida"
    assert etapas_prontas(ETAPAS_SEMENTE, estado) == ["coleta_dados"]


def test_gate_pendente_bloqueia_dependente():
    tarefas = [tarefa("contexto", 1, "concluida"), tarefa("coleta_dados", 1, "concluida")]
    marcos = [marco("M1", "coleta_dados", "aguardando_gate")]

    estado = estado_por_etapa(ETAPAS_SEMENTE, tarefas, marcos)

    assert estado["coleta_dados"] == "aguardando_gate"
    assert etapas_prontas(ETAPAS_SEMENTE, estado) == []


def test_gate_aprovado_conclui_e_libera():
    tarefas = [tarefa("contexto", 1, "concluida"), tarefa("coleta_dados", 1, "concluida")]
    estado = estado_por_etapa(ETAPAS_SEMENTE, tarefas, [marco("M1", "coleta_dados", "aprovado")])
    assert estado["coleta_dados"] == "concluida"
    assert etapas_prontas(ETAPAS_SEMENTE, estado) == ["harmonizacao"]


@pytest.mark.parametrize("situacao", ["reprovado", "em_andamento", "pendente", None])
def test_gate_nao_decidido_mantem_a_etapa_em_andamento(situacao):
    tarefas = [tarefa("contexto", 1, "concluida"), tarefa("coleta_dados", 1, "concluida")]
    marcos = [marco("M1", "coleta_dados", situacao)] if situacao else []
    estado = estado_por_etapa(ETAPAS_SEMENTE, tarefas, marcos)
    assert estado["coleta_dados"] == "em_andamento"
    assert etapas_prontas(ETAPAS_SEMENTE, estado) == []


def test_ultima_tentativa_prevalece():
    tarefas = [
        tarefa("contexto", 2, "em_execucao"),
        tarefa("contexto", 1, "falhou"),  # fora de ordem de propósito
    ]
    assert estado_por_etapa(ETAPAS_SEMENTE, tarefas, [])["contexto"] == "em_andamento"

    tarefas = [tarefa("contexto", 1, "concluida"), tarefa("contexto", 2, "falhou")]
    assert estado_por_etapa(ETAPAS_SEMENTE, tarefas, [])["contexto"] == "falhou"


@pytest.mark.parametrize(
    ("status", "esperado"),
    [
        ("pronta", "em_andamento"),
        ("em_execucao", "em_andamento"),
        ("validando", "em_andamento"),
        ("aguardando_gate", "aguardando_gate"),
        ("falhou", "falhou"),
        ("cancelada", "pendente"),
    ],
)
def test_mapeamento_do_status_da_tarefa(status, esperado):
    estado = estado_por_etapa(ETAPAS_SEMENTE, [tarefa("contexto", 1, status)], [])
    assert estado["contexto"] == esperado


def test_etapa_sem_tarefa_e_pendente_e_todas_aparecem():
    estado = estado_por_etapa(ETAPAS_SEMENTE, [], [])
    assert estado == _pendentes()


def test_etapa_cancelada_volta_a_ser_pronta_para_despacho():
    estado = estado_por_etapa(ETAPAS_SEMENTE, [tarefa("contexto", 1, "cancelada")], [])
    assert etapas_prontas(ETAPAS_SEMENTE, estado) == ["contexto"]


def test_etapa_falhou_nao_e_despachada_de_novo_pelo_dag():
    estado = estado_por_etapa(ETAPAS_SEMENTE, [tarefa("contexto", 1, "falhou")], [])
    assert etapas_prontas(ETAPAS_SEMENTE, estado) == []


def _etapa(codigo, ordem, deps=()):
    return Etapa(
        codigo=codigo,
        ordem=ordem,
        nome=codigo,
        descricao=None,
        depende_de=tuple(deps),
        papel_agente=PapelAgente("p", "prompt"),
        entradas=(),
        saidas=(),
        criterio_saida="c",
    )


def test_etapas_prontas_em_paralelo_seguem_a_ordem_das_etapas():
    etapas = [_etapa("c", 3, ["a"]), _etapa("a", 1), _etapa("b", 2, ["a"]), _etapa("d", 4, ["c"])]
    estado = {"a": "concluida", "b": "pendente", "c": "pendente", "d": "pendente"}
    assert etapas_prontas(etapas, estado) == ["b", "c"]


def test_etapa_em_andamento_nao_e_pronta_e_dependencia_pendente_bloqueia():
    etapas = [_etapa("a", 1), _etapa("b", 2, ["a"])]
    assert etapas_prontas(etapas, {"a": "em_andamento", "b": "pendente"}) == []


def test_validar_dag_detecta_ciclo():
    ciclo = [_etapa("a", 1, ["b"]), _etapa("b", 2, ["a"])]
    with pytest.raises(RegraViolada, match="Ciclo"):
        validar_dag(ciclo)
    validar_dag(ETAPAS_SEMENTE)  # a semente é um DAG
