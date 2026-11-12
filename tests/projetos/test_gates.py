import psycopg
import pytest

from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.projetos import servico
from tests.projetos.fabrica import (
    aprovar_todos_os_gates,
    consultar,
    em_execucao,
    ir_para_revisao,
    levar_a_gate,
    novo_projeto,
    preparar_fechamento,
    usuario,
)

DAVI = usuario("davi", "responsavel_por_dados")


def _marco(pid, codigo):
    return next(m for m in servico.marcos(pid) if m.codigo == codigo)


def test_gate_exige_papel(limpar):
    p = novo_projeto()
    levar_a_gate(p.id, "M1")
    dono = usuario("ana", "dono_do_projeto")  # M1 é do responsavel_por_dados
    with pytest.raises(Proibido, match="responsavel_por_dados"):
        servico.decidir_gate(p.id, "M1", "aprovado", "ok", dono)
    m = _marco(p.id, "M1")
    assert (m.status, m.revisor, m.decidido_em) == ("aguardando_gate", None, None)


def test_gate_exige_pessoa_designada(limpar):
    p = novo_projeto()  # responsavel_por_dados = davi
    levar_a_gate(p.id, "M1")
    outro = usuario("eva", "responsavel_por_dados")
    with pytest.raises(Proibido, match="davi"):
        servico.decidir_gate(p.id, "M1", "aprovado", "ok", outro)
    assert _marco(p.id, "M1").status == "aguardando_gate"

    m = servico.decidir_gate(p.id, "M1", "aprovado", "Dados completos", DAVI)
    assert (m.status, m.revisor, m.decisao_gate) == ("aprovado", "davi", "Dados completos")
    assert m.decidido_em is not None and m.data_real is not None


def test_gate_fora_de_aguardando_409(limpar):
    p = novo_projeto()
    em_execucao(p.id)
    with pytest.raises(RegraViolada, match="aguardando_gate"):  # pendente
        servico.decidir_gate(p.id, "M1", "aprovado", "ok", DAVI)
    levar_a_gate(p.id, "M1")
    servico.decidir_gate(p.id, "M1", "aprovado", "ok", DAVI)
    with pytest.raises(RegraViolada, match="aguardando_gate"):  # já decidido
        servico.decidir_gate(p.id, "M1", "reprovado", "mudei de ideia", DAVI)
    assert _marco(p.id, "M1").status == "aprovado"


def test_gate_nao_autorizado_recebe_proibido_mesmo_fora_de_aguardando(limpar):
    p = novo_projeto()  # M1 pendente: o intruso recebe Proibido, não 409
    with pytest.raises(Proibido):
        servico.decidir_gate(p.id, "M1", "aprovado", "ok", usuario("ana", "dono_do_projeto"))


def test_marco_sem_gate_nao_aceita_decidir_gate(limpar):
    p = novo_projeto()
    em_execucao(p.id)
    servico.atualizar_marco(p.id, "M2", "em_andamento", "sistema")
    with pytest.raises(RegraViolada, match="não exige gate"):
        servico.decidir_gate(p.id, "M2", "aprovado", "ok", usuario("ana", "dono_do_projeto"))
    assert _marco(p.id, "M2").status == "em_andamento"


def test_marco_inexistente_e_nao_encontrado(limpar):
    p = novo_projeto()
    with pytest.raises(NaoEncontrado):
        servico.decidir_gate(p.id, "M9", "aprovado", "ok", DAVI)


def test_reprovar_exige_comentario_e_o_marco_pode_ser_refeito(limpar, banco):
    p = novo_projeto()
    levar_a_gate(p.id, "M3")
    rita = usuario("rita", "revisor_metodologico")
    with pytest.raises(RegraViolada, match="comentário"):
        servico.decidir_gate(p.id, "M3", "reprovado", " ", rita)

    m = servico.decidir_gate(p.id, "M3", "reprovado", "Faltam intervalos de incerteza", rita)
    assert (m.status, m.revisor, m.decisao_gate) == (
        "reprovado",
        "rita",
        "Faltam intervalos de incerteza",
    )

    # a execução reabre a etapa; o gate volta a ser decidido depois
    servico.atualizar_marco(p.id, "M3", "em_andamento", "sistema")
    servico.atualizar_marco(p.id, "M3", "aguardando_gate", "sistema")
    # o sistema não aprova gate, nem reaprovando um marco que já teve revisor
    with pytest.raises(RegraViolada, match="exige gate"):
        servico.atualizar_marco(p.id, "M3", "aprovado", "sistema")
    assert servico.decidir_gate(p.id, "M3", "aprovado", "Agora sim", rita).status == "aprovado"

    # trilha: um evento por mudança de status do marco, com o autor de cada um
    eventos = consultar(
        banco,
        "select evento, de_status, para_status, motivo, autor from projetos.projeto_evento "
        "where starts_with(evento, 'marco_') order by id",
    )
    assert eventos == [
        ("marco_em_andamento", "pendente", "em_andamento", "M3", "sistema"),
        ("marco_aguardando_gate", "em_andamento", "aguardando_gate", "M3", "sistema"),
        ("marco_reprovado", "aguardando_gate", "reprovado", "M3", "rita"),
        ("marco_em_andamento", "reprovado", "em_andamento", "M3", "sistema"),
        ("marco_aguardando_gate", "em_andamento", "aguardando_gate", "M3", "sistema"),
        ("marco_aprovado", "aguardando_gate", "aprovado", "M3", "rita"),
    ]
    (payload,) = consultar(
        banco,
        "select payload from projetos.outbox where tipo_evento = 'MarcoStatusAlterado' "
        "and payload->>'para' = 'reprovado'",
    )
    assert payload[0] == {
        "codigo": "M3",
        "etapa_codigo": "modelagem",
        "de": "aguardando_gate",
        "para": "reprovado",
        "decisao_gate": "Faltam intervalos de incerteza",
    }
    agregados = consultar(
        banco,
        "select distinct agregado_id from projetos.outbox where starts_with(tipo_evento, 'Marco')",
    )
    assert agregados == [(p.id,)]


# --- R14: gates e marcos só com o projeto em andamento -------------------------------------
@pytest.mark.parametrize("status", ["aprovado", "pausado", "cancelado"])
def test_gate_exige_projeto_em_execucao_ou_em_revisao(limpar, status):
    p = novo_projeto()
    if status != "aprovado":
        levar_a_gate(p.id, "M1")
        servico.transicionar(p.id, status, "ana", "motivo")
    with pytest.raises(RegraViolada, match="em_execucao"):
        servico.decidir_gate(p.id, "M1", "aprovado", "ok", DAVI)
    assert _marco(p.id, "M1").revisor is None


def test_gate_pode_ser_decidido_em_revisao(limpar):
    p = novo_projeto()
    levar_a_gate(p.id, "M1")
    servico.transicionar(p.id, "em_revisao", "ana")
    assert servico.decidir_gate(p.id, "M1", "aprovado", "ok", DAVI).status == "aprovado"


def test_gate_de_projeto_excluido_e_nao_encontrado(limpar, banco):
    p = novo_projeto()
    levar_a_gate(p.id, "M1")
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute("update projetos.projeto set excluido_em = now() where id = %s", [p.id])
    with pytest.raises(NaoEncontrado):
        servico.decidir_gate(p.id, "M1", "aprovado", "ok", DAVI)


@pytest.mark.parametrize("status", ["aprovado", "pausado", "em_revisao"])
def test_atualizar_marco_exige_projeto_em_execucao(limpar, status):
    p = novo_projeto()
    if status != "aprovado":
        em_execucao(p.id)
        servico.transicionar(p.id, status, "ana", "motivo")
    with pytest.raises(RegraViolada, match="em_execucao"):
        servico.atualizar_marco(p.id, "M2", "em_andamento", "sistema")
    assert _marco(p.id, "M2").status == "pendente"


def test_concluir_sem_licoes_409(limpar):
    p = novo_projeto()
    aprovar_todos_os_gates(p.id)
    ir_para_revisao(p.id)
    with pytest.raises(RegraViolada, match="lições aprendidas"):
        servico.transicionar(p.id, "concluido", "ana")
    servico.registrar_licao(p.id, "funcionou", "x", None)
    with pytest.raises(RegraViolada, match="próximos passos"):
        servico.transicionar(p.id, "concluido", "ana")
    servico.registrar_proximo_passo(p.id, "Estender")  # sem ideia gerada nem descarte
    with pytest.raises(RegraViolada, match="sem retroalimentação"):
        servico.transicionar(p.id, "concluido", "ana")


def test_concluir_exige_gates_aprovados(limpar):
    p = novo_projeto()
    servico.registrar_licao(p.id, "funcionou", "x", None)
    passo = servico.registrar_proximo_passo(p.id, "y")
    servico.descartar_proximo_passo(passo, "sem interesse", "carla")
    ir_para_revisao(p.id)
    with pytest.raises(RegraViolada, match="gates não aprovados"):
        servico.transicionar(p.id, "concluido", "ana")


def test_concluir_com_tudo_em_ordem(limpar):
    p = novo_projeto()
    preparar_fechamento(p.id)
    assert servico.transicionar(p.id, "concluido", "ana").status == "concluido"


def test_marco_transicao_invalida_409(limpar):
    p = novo_projeto()
    em_execucao(p.id)
    # pendente -> aprovado com exige_gate: o serviço recusa...
    with pytest.raises(RegraViolada):
        servico.atualizar_marco(p.id, "M1", "aprovado", "sistema")
    # ...e o trigger também: pendente -> aguardando_gate (aqui em marco sem gate)
    with pytest.raises(RegraViolada, match="Transição de status de marco inválida"):
        servico.atualizar_marco(p.id, "M2", "aguardando_gate", "sistema")
    assert _marco(p.id, "M1").status == "pendente"


def _forcar_status(conn, projeto_id, codigo, status):
    """Coloca o marco num status sem passar pelo trigger (só nestes testes de SQL direto)."""
    conn.execute("alter table projetos.marco disable trigger marco_transicao")
    conn.execute(
        "update projetos.marco set status = %s, revisor = 'x', decidido_em = now() "
        "where projeto_id = %s and codigo = %s",
        [status, projeto_id, codigo],
    )
    conn.execute("alter table projetos.marco enable trigger marco_transicao")


@pytest.mark.parametrize(
    ("origem", "destino", "codigo"),
    [
        ("pendente", "aprovado", "M1"),
        ("pendente", "aguardando_gate", "M1"),
        ("em_andamento", "aprovado", "M1"),  # exige gate: só via aguardando_gate
        ("em_andamento", "reprovado", "M1"),
        ("em_andamento", "aguardando_gate", "M2"),  # M2 não exige gate
        ("em_andamento", "pendente", "M2"),
        ("aprovado", "em_andamento", "M2"),
        ("aprovado", "cancelado", "M2"),
        ("cancelado", "pendente", "M2"),
        ("reprovado", "aprovado", "M1"),
    ],
)
def test_trigger_do_marco_recusa_transicao_invalida(limpar, banco, origem, destino, codigo):
    p = novo_projeto()
    with psycopg.connect(banco, autocommit=True) as c:
        _forcar_status(c, p.id, codigo, origem)
        with pytest.raises(psycopg.errors.CheckViolation, match="Transição de status de marco"):
            c.execute(
                "update projetos.marco set status = %s where projeto_id = %s and codigo = %s",
                [destino, p.id, codigo],
            )


@pytest.mark.parametrize(
    ("origem", "destino", "codigo"),
    [
        ("pendente", "em_andamento", "M2"),
        ("em_andamento", "aprovado", "M2"),  # sem gate
        ("em_andamento", "aguardando_gate", "M1"),
        ("aguardando_gate", "aprovado", "M1"),
        ("aguardando_gate", "reprovado", "M1"),
        ("reprovado", "em_andamento", "M1"),
        ("reprovado", "cancelado", "M1"),
        ("pendente", "cancelado", "M2"),
        ("em_andamento", "cancelado", "M2"),
        ("aguardando_gate", "cancelado", "M1"),
    ],
)
def test_trigger_do_marco_aceita_transicao_valida(limpar, banco, origem, destino, codigo):
    p = novo_projeto()
    with psycopg.connect(banco, autocommit=True) as c:
        _forcar_status(c, p.id, codigo, origem)
        c.execute(
            "update projetos.marco set status = %s, revisor = 'x', decidido_em = now() "
            "where projeto_id = %s and codigo = %s",
            [destino, p.id, codigo],
        )
        (status,) = c.execute(
            "select status from projetos.marco where projeto_id = %s and codigo = %s",
            [p.id, codigo],
        ).fetchone()
    assert status == destino
