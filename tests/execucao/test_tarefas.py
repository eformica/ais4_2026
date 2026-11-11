from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from ais4.comum.erros import NaoEncontrado, RegraViolada
from ais4.execucao import servico
from tests.execucao.conftest import consultar


def _criar(projeto_id, etapa="contexto", **kw):
    campos = {
        "papel": "pesquisador",
        "modelo": "claude-opus-5-5",
        "entradas": {"pergunta": "x"},
        "semente": 42,
    }
    campos.update(kw)
    return servico.criar_tarefa(projeto_id, etapa, **campos)


def _em_execucao(projeto_id, etapa="contexto"):
    t = _criar(projeto_id, etapa)
    assert servico.reservar(t.id, 60)
    return t


# --- criar_tarefa / tarefas ----------------------------------------------------------------
def test_criar_tarefa_nasce_pronta_na_primeira_tentativa(projeto_id):
    t = _criar(projeto_id)

    assert t.status == "pronta" and t.tentativa == 1
    assert (t.projeto_id, t.etapa_codigo, t.papel, t.modelo) == (
        projeto_id,
        "contexto",
        "pesquisador",
        "claude-opus-5-5",
    )
    assert t.entradas == {"pergunta": "x"} and t.semente == 42
    assert t.saidas is None and t.lease_ate is None and t.finalizado_em is None
    assert servico.obter_tarefa(t.id) == t


def test_reexecucao_cria_nova_tentativa_sem_apagar(projeto_id, banco):
    primeira = _em_execucao(projeto_id)
    servico.finalizar(primeira.id, "falhou", erro="boom", feedback=["corrija"])

    segunda = _criar(projeto_id)

    assert (primeira.tentativa, segunda.tentativa) == (1, 2)
    todas = servico.tarefas(projeto_id)
    assert [(t.tentativa, t.status) for t in todas] == [(1, "falhou"), (2, "pronta")]
    assert todas[0].erro == "boom" and todas[0].feedback == ["corrija"]  # a anterior intacta
    assert consultar(banco, "select count(*) from execucao.tarefa")[0][0] == 2


def test_tentativas_sao_contadas_por_etapa_e_projeto(projeto_id):
    assert _criar(projeto_id, "contexto").tentativa == 1
    assert _criar(projeto_id, "contexto").tentativa == 2
    assert _criar(projeto_id, "coleta_dados").tentativa == 1


def test_tarefas_ordena_por_etapa_e_tentativa(projeto_id):
    _criar(projeto_id, "coleta_dados")
    _criar(projeto_id, "contexto")
    _criar(projeto_id, "contexto")

    ordem = [(t.etapa_codigo, t.tentativa) for t in servico.tarefas(projeto_id)]

    assert ordem == [("coleta_dados", 1), ("contexto", 1), ("contexto", 2)]


def test_tarefa_de_projeto_inexistente_e_regra_violada(banco, limpar):
    with pytest.raises(RegraViolada):
        _criar(uuid4())


def test_obter_tarefa_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        servico.obter_tarefa(uuid4())


# --- reservar / renovar_lease --------------------------------------------------------------
def test_reservar_ganha_uma_vez_e_define_lease_e_inicio(projeto_id):
    t = _criar(projeto_id)
    antes = datetime.now(UTC)

    assert servico.reservar(t.id, 120) is True
    assert servico.reservar(t.id, 120) is False  # já em_execucao

    r = servico.obter_tarefa(t.id)
    assert r.status == "em_execucao"
    assert r.iniciado_em is not None and r.iniciado_em >= antes - timedelta(seconds=5)
    assert timedelta(seconds=100) < r.lease_ate - r.iniciado_em <= timedelta(seconds=121)


def test_reservar_so_a_tarefa_pronta(projeto_id):
    t = _em_execucao(projeto_id)
    servico.finalizar(t.id, "falhou", erro="x")
    assert servico.reservar(t.id, 60) is False
    assert servico.reservar(uuid4(), 60) is False


def test_reservar_concorrente_so_um_ganha(projeto_id):
    t = _criar(projeto_id)

    with ThreadPoolExecutor(8) as pool:
        resultados = list(pool.map(lambda _: servico.reservar(t.id, 60), range(8)))

    assert resultados.count(True) == 1


def test_reservar_lease_nao_positivo(projeto_id):
    t = _criar(projeto_id)
    with pytest.raises(RegraViolada):
        servico.reservar(t.id, 0)
    assert servico.obter_tarefa(t.id).status == "pronta"


def test_renovar_lease_estende_enquanto_em_execucao(projeto_id):
    t = _em_execucao(projeto_id)
    antes = servico.obter_tarefa(t.id).lease_ate

    assert servico.renovar_lease(t.id, 3600) is True

    assert servico.obter_tarefa(t.id).lease_ate > antes + timedelta(minutes=50)


def test_renovar_lease_de_quem_perdeu_a_tarefa_devolve_falso(projeto_id):
    pronta = _criar(projeto_id, "coleta_dados")
    assert servico.renovar_lease(pronta.id, 60) is False
    t = _em_execucao(projeto_id)
    servico.finalizar(t.id, "falhou", erro="x")
    assert servico.renovar_lease(t.id, 60) is False


# --- finalizar ------------------------------------------------------------------------------
def test_finalizar_conclui_com_saidas_ambiente_e_limpa_o_lease(projeto_id):
    t = _em_execucao(projeto_id)
    servico.finalizar(t.id, "validando", saidas={"artefatos": []})
    depois_da_execucao = servico.obter_tarefa(t.id)
    assert depois_da_execucao.status == "validando"
    assert depois_da_execucao.lease_ate is None and depois_da_execucao.finalizado_em is None

    f = servico.finalizar(t.id, "concluida", ambiente={"python": "3.12", "pandas": "2.2"})

    assert f.status == "concluida"
    assert f.saidas == {"artefatos": []}  # o que já estava gravado fica
    assert f.ambiente == {"python": "3.12", "pandas": "2.2"}
    assert f.finalizado_em is not None and f.lease_ate is None


def test_finalizar_falha_direto_da_execucao_com_erro(projeto_id):
    t = _em_execucao(projeto_id)

    f = servico.finalizar(t.id, "falhou", erro="lease_expirado", feedback={"motivo": "x"})

    assert (f.status, f.erro, f.feedback) == ("falhou", "lease_expirado", {"motivo": "x"})
    assert f.finalizado_em is not None and f.lease_ate is None


def test_finalizar_caminho_do_gate(projeto_id):
    t = _em_execucao(projeto_id)
    servico.finalizar(t.id, "validando", saidas={"a": 1})
    assert servico.finalizar(t.id, "aguardando_gate").finalizado_em is None
    assert servico.finalizar(t.id, "concluida").status == "concluida"


def test_finalizar_transicao_invalida_e_regra_violada(projeto_id):
    pronta = _criar(projeto_id)
    with pytest.raises(RegraViolada, match="pronta -> concluida"):
        servico.finalizar(pronta.id, "concluida")
    t = _em_execucao(projeto_id, "coleta_dados")
    with pytest.raises(RegraViolada, match="em_execucao -> concluida"):
        servico.finalizar(t.id, "concluida")  # tem de passar por validando
    assert servico.obter_tarefa(t.id).status == "em_execucao"


def test_finalizar_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        servico.finalizar(uuid4(), "falhou", erro="x")


def test_terminal_nao_muda_de_status_nem_de_saidas(projeto_id):
    t = _em_execucao(projeto_id)
    servico.finalizar(t.id, "validando", saidas={"v": 1})
    servico.finalizar(t.id, "concluida")

    with pytest.raises(RegraViolada, match="terminal"):
        servico.finalizar(t.id, "falhou", erro="tarde demais")
    with pytest.raises(RegraViolada, match="imutáveis"):
        servico.finalizar(t.id, "concluida", saidas={"v": 2})
    assert servico.obter_tarefa(t.id).saidas == {"v": 1}


# --- trigger direto no banco -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("caminho", "alvo", "valido"),
    [
        (["em_execucao"], "cancelada", True),
        ([], "cancelada", True),
        (["em_execucao", "validando", "aguardando_gate"], "cancelada", True),
        (["em_execucao", "validando", "aguardando_gate"], "falhou", True),
        (["em_execucao"], "pronta", False),
        (["em_execucao", "validando"], "em_execucao", False),
        (["em_execucao", "validando"], "pronta", False),
        ([], "validando", False),
        ([], "aguardando_gate", False),
        (["em_execucao", "falhou"], "pronta", False),
        (["em_execucao", "falhou"], "cancelada", False),
        (["em_execucao", "validando", "concluida"], "falhou", False),
        (["cancelada"], "pronta", False),
    ],
)
def test_transicoes_de_status_no_banco(projeto_id, banco, caminho, alvo, valido):
    t = _criar(projeto_id)
    with psycopg.connect(banco, autocommit=True) as c:
        for passo in caminho:
            c.execute("update execucao.tarefa set status = %s where id = %s", (passo, t.id))
        if valido:
            c.execute("update execucao.tarefa set status = %s where id = %s", (alvo, t.id))
            (status,) = c.execute(
                "select status from execucao.tarefa where id = %s", (t.id,)
            ).fetchone()
            assert status == alvo
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("update execucao.tarefa set status = %s where id = %s", (alvo, t.id))


@pytest.mark.parametrize("campo", ["entradas", "saidas", "feedback"])
def test_terminal_protege_entradas_saidas_e_feedback_no_banco(projeto_id, banco, campo):
    t = _em_execucao(projeto_id)
    servico.finalizar(t.id, "falhou", erro="x")
    with psycopg.connect(banco) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute(f"update execucao.tarefa set {campo} = '{{\"novo\": 1}}' where id = %s", (t.id,))


def test_cancelar_pelo_banco_preenche_finalizado_em_e_limpa_o_lease(projeto_id, banco):
    t = _em_execucao(projeto_id)
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute("update execucao.tarefa set status = 'cancelada' where id = %s", (t.id,))
    r = servico.obter_tarefa(t.id)
    assert r.status == "cancelada" and r.finalizado_em is not None and r.lease_ate is None


def test_tentativa_duplicada_e_rejeitada_pelo_banco(projeto_id, banco):
    t = _criar(projeto_id)
    with psycopg.connect(banco) as c, pytest.raises(psycopg.errors.UniqueViolation):
        c.execute(
            "insert into execucao.tarefa (projeto_id, etapa_codigo, tentativa, papel, modelo) "
            "values (%s, 'contexto', %s, 'p', 'm')",
            (projeto_id, t.tentativa),
        )
