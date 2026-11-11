from datetime import date
from uuid import uuid4

import psycopg
import pytest
from pydantic import ValidationError

from ais4.comum.dto import ArtefatoRef
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.comum.espec import MarcoPlanejado
from ais4.projetos import servico
from tests.playbooks.semente import IDEIA_SEMENTE
from tests.projetos.fabrica import (
    PLAYBOOK_ID,
    RESPONSAVEIS,
    comando,
    consultar,
    espec,
    ir_para_revisao,
    novo_projeto,
    preparar_fechamento,
    usuario,
)


def test_criar_projeto_cria_espec_marcos_responsaveis(limpar, banco):
    p = novo_projeto()

    assert p.status == "aprovado" and p.dono == "ana" and p.playbook_versao == "0.1.0"
    assert servico.obter(p.id) == p

    versoes = consultar(banco, "select versao, aprovado_por from projetos.especificacao")
    assert versoes == [(1, "ana")]

    marcos = servico.marcos(p.id)
    assert [m.codigo for m in marcos] == ["M1", "M2", "M3", "M4", "M5", "M6"]
    assert [m.ordem for m in marcos] == [1, 2, 3, 4, 5, 6]
    assert all(m.status == "pendente" for m in marcos)
    planejado = {m.codigo: m for m in espec().cronograma}
    for m in marcos:
        esperado = planejado[m.codigo]
        assert (m.nome, m.etapa_codigo, m.criterio_saida, m.exige_gate, m.papel_revisor) == (
            esperado.nome,
            esperado.etapa_codigo,
            esperado.criterio_saida,
            esperado.exige_gate,
            esperado.papel_revisor,
        )
    assert [m.codigo for m in marcos if m.exige_gate] == ["M1", "M3", "M5", "M6"]

    resp = dict(consultar(banco, "select papel, pessoa from projetos.responsavel"))
    assert resp == RESPONSAVEIS

    # o trigger existente registra a criação no outbox
    assert consultar(banco, "select tipo_evento from projetos.outbox") == [("ProjetoCriado",)]


def test_criar_projeto_copia_data_planejada_do_cronograma(limpar):
    base = espec()
    cronograma = [
        base.cronograma[0].model_copy(update={"data_planejada": date(2027, 3, 1)}),
        *base.cronograma[1:],
    ]
    p = novo_projeto(especificacao=base.model_copy(update={"cronograma": cronograma}))
    assert servico.marcos(p.id)[0].data_planejada == date(2027, 3, 1)


def test_criar_projeto_e_atomico(limpar, banco):
    duplicado = espec().cronograma[0]  # dois marcos M1: viola UNIQUE(projeto_id, codigo)
    ruim = espec().model_copy(update={"cronograma": [duplicado, duplicado]})
    with pytest.raises(RegraViolada):
        novo_projeto(especificacao=ruim)
    for tabela in ("projeto", "especificacao", "marco", "responsavel", "outbox"):
        assert consultar(banco, f"select count(*) from projetos.{tabela}") == [(0,)], tabela


def test_criar_projeto_gate_sem_papel_revisor_e_ideia_inexistente_sao_409(limpar, banco):
    sem_papel = MarcoPlanejado(
        codigo="MX",
        nome="x",
        criterio_saida="y",
        exige_gate=True,
        papel_revisor=None,
        responsavel="humano:dono_do_projeto",
    )
    with pytest.raises(RegraViolada):
        novo_projeto(especificacao=espec().model_copy(update={"cronograma": [sem_papel]}))
    with pytest.raises(RegraViolada):
        novo_projeto(ideia_origem_id=uuid4())
    assert consultar(banco, "select count(*) from projetos.projeto") == [(0,)]


def test_criar_projeto_designa_o_dono_quando_responsaveis_nao_o_inclui(limpar, banco):
    sem_dono = {k: v for k, v in RESPONSAVEIS.items() if k != "dono_do_projeto"}
    novo_projeto(dono="bruno", responsaveis=sem_dono)
    resp = dict(consultar(banco, "select papel, pessoa from projetos.responsavel"))
    assert resp == {**sem_dono, "dono_do_projeto": "bruno"}


def test_criar_projeto_nao_sobrescreve_dono_informado_em_responsaveis(limpar, banco):
    novo_projeto(dono="ana", responsaveis={**RESPONSAVEIS, "dono_do_projeto": "bruno"})
    resp = dict(consultar(banco, "select papel, pessoa from projetos.responsavel"))
    assert resp["dono_do_projeto"] == "bruno"


def test_criar_projeto_exige_responsavel_para_todo_papel_revisor_de_gate(limpar, banco):
    faltando = {k: v for k, v in RESPONSAVEIS.items() if k != "revisor_metodologico"}
    with pytest.raises(RegraViolada, match="revisor_metodologico"):
        novo_projeto(responsaveis=faltando)
    for tabela in ("projeto", "especificacao", "marco", "responsavel"):
        assert consultar(banco, f"select count(*) from projetos.{tabela}") == [(0,)], tabela


def test_criar_projeto_com_id_e_idempotente(limpar, banco):
    id_fixo = uuid4()
    primeiro = novo_projeto(id=id_fixo)
    segundo = novo_projeto(id=id_fixo, titulo="Outro título, ignorado")

    assert primeiro.id == id_fixo and segundo == primeiro
    for tabela in ("projeto", "especificacao", "marco", "responsavel", "outbox"):
        esperado = {"projeto": 1, "especificacao": 1, "marco": 6, "responsavel": 4, "outbox": 1}
        assert consultar(banco, f"select count(*) from projetos.{tabela}") == [
            (esperado[tabela],)
        ], tabela


def test_criar_projeto_com_id_existente_de_outra_ideia_e_409(limpar):
    id_fixo = uuid4()
    novo_projeto(id=id_fixo)
    with pytest.raises(RegraViolada, match="outra ideia"):
        novo_projeto(id=id_fixo, ideia_origem_id=uuid4())


def test_criar_projeto_rejeita_papel_desconhecido_em_responsaveis(limpar):
    with pytest.raises(ValidationError):
        comando(responsaveis={"revisor_de_dados": "davi"})


def test_leituras_e_escritas_de_projeto_inexistente_sao_nao_encontrado(limpar):
    pid = uuid4()
    for chamada in (
        lambda: servico.obter(pid),
        lambda: servico.marcos(pid),
        lambda: servico.marco_por_etapa(pid, "coleta_dados"),
        lambda: servico.obter_especificacao(pid),
        lambda: servico.artefatos(pid),
        lambda: servico.licoes(pid),
        lambda: servico.proximos_passos(pid),
        lambda: servico.registrar_licao(pid, "funcionou", "x", None),
        lambda: servico.registrar_proximo_passo(pid, "x"),
        lambda: servico.nova_versao_especificacao(pid, espec(), "j", "ana"),
        lambda: servico.transicionar(pid, "em_execucao", "ana"),
        lambda: servico.atualizar_marco(pid, "M1", "em_andamento", "sistema"),
        lambda: servico.registrar_avaliacao(pid, "c", 3, None, usuario("ana", "dono_do_projeto")),
    ):
        with pytest.raises(NaoEncontrado):
            chamada()


def test_listar_filtra_por_status(limpar):
    a = novo_projeto(titulo="A")
    b = novo_projeto(titulo="B")
    servico.transicionar(b.id, "em_execucao", "ana")
    assert {p.id for p in servico.listar()} == {a.id, b.id}
    assert [p.id for p in servico.listar("em_execucao")] == [b.id]
    assert len(servico.listar(limite=1)) == 1


def test_nova_versao_sem_justificativa_409(limpar, banco):
    p = novo_projeto()
    for vazia in ("", "   "):
        with pytest.raises(RegraViolada, match="justificativa"):
            servico.nova_versao_especificacao(p.id, espec(), vazia, "ana")
    assert consultar(banco, "select count(*) from projetos.especificacao") == [(1,)]


def test_nova_versao_incrementa_e_vigente_e_a_mais_alta(limpar):
    p = novo_projeto()
    v1 = servico.especificacao_vigente(p.id)
    assert v1 == espec()  # ida e volta pelo JSONB preserva o contrato

    novo = espec().model_copy(
        update={"escopo": espec().escopo.model_copy(update={"fora": ["Séries pré-2000"]})}
    )
    assert servico.nova_versao_especificacao(p.id, novo, "Reduzir escopo", "bruno") == 2
    assert servico.nova_versao_especificacao(p.id, novo, "Mais um ajuste", "bruno") == 3

    assert servico.especificacao_vigente(p.id) == novo
    dto = servico.obter_especificacao(p.id, 2)
    assert (dto.versao, dto.justificativa, dto.aprovado_por) == (2, "Reduzir escopo", "bruno")
    assert servico.obter_especificacao(p.id, 1).especificacao == v1
    assert servico.obter_especificacao(p.id, 1).justificativa is None
    with pytest.raises(NaoEncontrado):
        servico.obter_especificacao(p.id, 9)


def test_versoes_sao_imutaveis(limpar, banco):
    novo_projeto()
    with psycopg.connect(banco) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("update projetos.especificacao set justificativa = 'x'")


def test_transicionar_projeto(limpar):
    p = novo_projeto()
    assert servico.transicionar(p.id, "em_execucao", "ana").status == "em_execucao"
    pausado = servico.transicionar(p.id, "pausado", "ana", "Orçamento estourado")
    assert (pausado.status, pausado.motivo_status) == ("pausado", "Orçamento estourado")
    assert servico.transicionar(p.id, "em_execucao", "ana").motivo_status is None
    with pytest.raises(RegraViolada, match="já está"):
        servico.transicionar(p.id, "em_execucao", "ana")
    with pytest.raises(RegraViolada, match="Transição de status de projeto inválida"):
        servico.transicionar(p.id, "concluido", "ana")
    with pytest.raises(RegraViolada):  # pausar sem motivo
        servico.transicionar(p.id, "pausado", "ana")


def test_marco_por_etapa(limpar):
    p = novo_projeto()
    m = servico.marco_por_etapa(p.id, "modelagem")
    assert m is not None and m.codigo == "M3"
    assert servico.marco_por_etapa(p.id, "harmonizacao") is None


def test_atualizar_marco_e_so_para_status_do_sistema(limpar):
    p = novo_projeto()
    assert servico.atualizar_marco(p.id, "M2", "em_andamento", "sistema").status == "em_andamento"
    aprovado = servico.atualizar_marco(p.id, "M2", "aprovado", "sistema")  # sem gate
    assert aprovado.status == "aprovado" and aprovado.data_real is not None
    for indevido in ("reprovado", "cancelado", "pendente"):
        with pytest.raises(RegraViolada, match="só move marcos"):
            servico.atualizar_marco(p.id, "M4", indevido, "sistema")
    with pytest.raises(NaoEncontrado):
        servico.atualizar_marco(p.id, "M9", "em_andamento", "sistema")


def test_artefatos(limpar):
    p = novo_projeto()
    m1 = servico.marcos(p.id)[0]
    ref = ArtefatoRef(uri="file://x", sha256="a" * 64, nome="dados.csv", tipo="dataset")
    aid = servico.registrar_artefato(p.id, m1.id, "dataset", "dados.csv", ref, {"etapa": "coleta"})
    solto = servico.registrar_artefato(p.id, None, "outro", "nota", ref, {})
    lista = servico.artefatos(p.id)
    assert {a.id for a in lista} == {aid, solto}
    a = next(a for a in lista if a.id == aid)
    assert (a.marco_id, a.tipo, a.nome, a.uri, a.hash_sha256, a.proveniencia) == (
        m1.id,
        "dataset",
        "dados.csv",
        "file://x",
        "a" * 64,
        {"etapa": "coleta"},
    )
    invalido = ArtefatoRef(uri="file://x", sha256="zz", nome="n", tipo="outro")
    with pytest.raises(RegraViolada):  # CHECK do hash
        servico.registrar_artefato(p.id, None, "outro", "n", invalido, {})


def test_licoes_e_proximos_passos(limpar):
    p = novo_projeto()
    lid = servico.registrar_licao(p.id, "falhou", "Séries incompatíveis", PLAYBOOK_ID)
    (licao,) = servico.licoes(p.id)
    assert (licao.id, licao.tipo, licao.playbook_alvo_id, licao.encaminhada_em) == (
        lid,
        "falhou",
        PLAYBOOK_ID,
        None,
    )
    servico.marcar_licao_encaminhada(lid)
    primeira = servico.licoes(p.id)[0].encaminhada_em
    assert primeira is not None
    servico.marcar_licao_encaminhada(lid)  # idempotente: não muda a data
    assert servico.licoes(p.id)[0].encaminhada_em == primeira
    with pytest.raises(NaoEncontrado):
        servico.marcar_licao_encaminhada(uuid4())
    with pytest.raises(RegraViolada):  # tipo fora do CHECK (contornando o Literal)
        servico.registrar_licao(p.id, "xyz", "d", None)  # type: ignore[arg-type]

    pid = servico.registrar_proximo_passo(p.id, "Estender a 2060")
    (passo,) = servico.proximos_passos(p.id)
    assert (passo.id, passo.descricao, passo.ideia_gerada_id, passo.descartado_motivo) == (
        pid,
        "Estender a 2060",
        None,
        None,
    )


def test_vincular_ideia_e_descartar_proximo_passo(limpar):
    p = novo_projeto()
    a = servico.registrar_proximo_passo(p.id, "A")
    b = servico.registrar_proximo_passo(p.id, "B")

    servico.vincular_ideia_gerada(a, IDEIA_SEMENTE.id)
    servico.vincular_ideia_gerada(a, IDEIA_SEMENTE.id)  # idempotente
    assert servico.proximos_passos(p.id)[0].ideia_gerada_id == IDEIA_SEMENTE.id
    with pytest.raises(RegraViolada, match="já gerou"):
        servico.vincular_ideia_gerada(a, uuid4())
    with pytest.raises(RegraViolada):  # a mesma ideia não vira filha de dois passos (UNIQUE)
        servico.vincular_ideia_gerada(b, IDEIA_SEMENTE.id)
    with pytest.raises(RegraViolada):  # ideia inexistente (FK)
        servico.vincular_ideia_gerada(b, uuid4())
    with pytest.raises(NaoEncontrado):
        servico.vincular_ideia_gerada(uuid4(), IDEIA_SEMENTE.id)

    with pytest.raises(RegraViolada, match="motivo"):
        servico.descartar_proximo_passo(b, "  ", "carla")
    with pytest.raises(RegraViolada):  # já gerou ideia: não se descarta
        servico.descartar_proximo_passo(a, "x", "carla")
    descartado = servico.descartar_proximo_passo(b, "Fora de escopo", "carla")
    assert descartado.descartado_motivo == "Fora de escopo"
    with pytest.raises(RegraViolada, match="já foi descartado"):
        servico.descartar_proximo_passo(b, "de novo", "carla")
    with pytest.raises(RegraViolada):  # descartado não pode ser vinculado
        servico.vincular_ideia_gerada(b, IDEIA_SEMENTE.id)


def test_registrar_avaliacao_exige_papel_e_nota_valida(limpar):
    p = novo_projeto()
    av = servico.registrar_avaliacao(
        p.id, "rastreabilidade", 4, "bom", usuario("rita", "revisor_metodologico")
    )
    assert (av.criterio, av.nota, av.comentario, av.revisor) == (
        "rastreabilidade",
        4,
        "bom",
        "rita",
    )
    with pytest.raises(Proibido):
        servico.registrar_avaliacao(
            p.id, "incerteza", 4, None, usuario("carla", "curador_do_acervo")
        )
    with pytest.raises(RegraViolada):  # CHECK 1..5
        servico.registrar_avaliacao(p.id, "incerteza", 6, None, usuario("ana", "dono_do_projeto"))


def test_avaliacoes_por_playbook_so_conta_projetos_concluidos(limpar):
    concluido = novo_projeto(titulo="Concluído")
    aberto = novo_projeto(titulo="Aberto")
    em_revisao = novo_projeto(titulo="Em revisão")
    ana = usuario("ana", "dono_do_projeto")
    for pid, notas in ((concluido.id, (5, 4, 3)), (aberto.id, (1,)), (em_revisao.id, (2,))):
        for i, nota in enumerate(notas):
            servico.registrar_avaliacao(pid, f"c{i}", nota, None, ana)
    ir_para_revisao(em_revisao.id)
    preparar_fechamento(concluido.id)
    servico.transicionar(concluido.id, "concluido", "ana")

    assert servico.avaliacoes_por_playbook(PLAYBOOK_ID, "0.1.0") == [(concluido.id, 4.0)]
    assert servico.avaliacoes_por_playbook(PLAYBOOK_ID, "9.9.9") == []
    assert servico.avaliacoes_por_playbook(uuid4(), "0.1.0") == []
