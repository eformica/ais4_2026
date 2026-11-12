from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError

from ais4.acervo import servico
from ais4.acervo.servico import IdeiaAtualizar, IdeiaCriar
from ais4.comum.dto import EventoDTO, IdeiaDTO
from ais4.comum.erros import NaoEncontrado, RegraViolada
from tests.playbooks.semente import IDEIA_SEMENTE

FONTE = [{"nome": "IBGE - Censo 2022"}]


def _dados(**kw) -> IdeiaCriar:
    campos = {
        "titulo": "Ideia de teste",
        "tipo": "pergunta_de_pesquisa",
        "descricao": "Contexto da ideia.",
        "pergunta_norteadora": "Qual é a pergunta?",
        "dominio": ["agro", "demografia"],
        "valor": 3,
        "viabilidade": 4,
    }
    campos.update(kw)
    return IdeiaCriar(**campos)


def _qualificada(usuario="ana", **kw) -> IdeiaDTO:
    """Ideia que já passou por rascunho -> triagem -> qualificada (precisa de fonte)."""
    kw.setdefault("fontes_candidatas", FONTE)
    ideia = servico.criar_ideia(_dados(status="triagem", **kw), usuario)
    return servico.transicionar(ideia.id, "qualificada", usuario)


def _consultar(banco, sql, params=()):
    with psycopg.connect(banco) as c:
        return c.execute(sql, params).fetchall()


# --- validação de IdeiaCriar --------------------------------------------------------------
def test_ideiacriar_rejeita_titulo_longo_e_notas_fora_da_faixa():
    with pytest.raises(ValidationError):
        _dados(titulo="x" * 101)
    assert _dados(titulo="x" * 100).titulo == "x" * 100
    for campo in ("valor", "viabilidade"):
        for nota in (0, 6):
            with pytest.raises(ValidationError):
                _dados(**{campo: nota})


def test_ideiacriar_fonte_externa_exige_url_e_dominio_nao_vazio():
    with pytest.raises(ValidationError):
        _dados(origem="fonte_externa")
    assert _dados(origem="fonte_externa", origem_url="https://exemplo.org").origem_url
    with pytest.raises(ValidationError):
        _dados(dominio=[])


def test_ideiaatualizar_rejeita_null_em_campo_obrigatorio():
    with pytest.raises(ValidationError):
        IdeiaAtualizar(titulo=None)
    assert IdeiaAtualizar(restricoes=None).model_fields_set == {"restricoes"}


# --- criar / obter ------------------------------------------------------------------------
def test_criar_e_obter(banco, limpar):
    criada = servico.criar_ideia(_dados(restricoes="Prazo curto"), "ana")
    assert isinstance(criada, IdeiaDTO)
    assert criada.status == "rascunho"
    assert criada.origem == "humana"
    assert criada.criado_por == "ana"
    assert criada.prioridade == Decimal(12)
    assert criada.prioridade_manual is None
    assert criada.dominio == ["agro", "demografia"]
    assert criada.fontes_candidatas == []
    assert criada.restricoes == "Prazo curto"
    assert criada.criado_em is not None
    assert servico.obter(criada.id) == criada


def test_criar_direto_em_triagem(banco, limpar):
    assert servico.criar_ideia(_dados(status="triagem"), "ana").status == "triagem"


def test_obter_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        servico.obter(uuid4())


def test_obter_ignora_excluida(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute("update acervo.ideia set excluido_em = now() where id = %s", [ideia.id])
    with pytest.raises(NaoEncontrado):
        servico.obter(ideia.id)


def test_semente_pode_ser_obtida(banco, limpar):
    assert servico.obter(IDEIA_SEMENTE.id).titulo == IDEIA_SEMENTE.titulo


# --- atualizar ----------------------------------------------------------------------------
def test_atualizar_so_altera_campos_informados(banco, limpar):
    ideia = servico.criar_ideia(_dados(restricoes="Prazo curto"), "ana")
    nova = servico.atualizar(
        ideia.id, IdeiaAtualizar(titulo="Outro título", valor=5, dominio=["logistica"]), "bia"
    )
    assert nova.titulo == "Outro título"
    assert nova.valor == 5
    assert nova.prioridade == Decimal(20)
    assert nova.dominio == ["logistica"]
    # o que não veio no PATCH não muda
    assert nova.restricoes == "Prazo curto"
    assert nova.descricao == ideia.descricao
    assert nova.status == "rascunho"
    assert nova.criado_por == "ana"
    assert nova.atualizado_em > ideia.atualizado_em


def test_atualizar_prioridade_manual_e_limpar(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    com_manual = servico.atualizar(ideia.id, IdeiaAtualizar(prioridade_manual=Decimal(30)), "ana")
    assert com_manual.prioridade == Decimal(30)
    sem_manual = servico.atualizar(ideia.id, IdeiaAtualizar(prioridade_manual=None), "ana")
    assert sem_manual.prioridade_manual is None
    assert sem_manual.prioridade == Decimal(12)


def test_atualizar_sem_campos_devolve_a_ideia(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    assert servico.atualizar(ideia.id, IdeiaAtualizar(), "ana") == ideia


def test_atualizar_registra_evento_editada_na_mesma_transacao(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    servico.atualizar(ideia.id, IdeiaAtualizar(viabilidade=1, descricao="Nova."), "bia")

    (evento,) = [e for e in servico.historico(ideia.id) if e.evento == "editada"]
    assert (evento.motivo, evento.autor, evento.de_status, evento.para_status) == (
        "descricao, viabilidade",
        "bia",
        None,
        None,
    )


def test_atualizar_ideia_arquivada_e_regra_violada(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    servico.transicionar(ideia.id, "arquivada", "carla", "fora do foco")
    with pytest.raises(RegraViolada, match="arquivada"):
        servico.atualizar(ideia.id, IdeiaAtualizar(titulo="x"), "carla")
    assert servico.obter(ideia.id).titulo == ideia.titulo


def test_atualizar_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        servico.atualizar(uuid4(), IdeiaAtualizar(titulo="x"), "ana")


# --- fila ---------------------------------------------------------------------------------
def test_fila_ordena_por_prioridade(banco, limpar):
    a = _qualificada(titulo="5x5", valor=5, viabilidade=5)
    b = _qualificada(titulo="2x2", valor=2, viabilidade=2)
    c = _qualificada(titulo="manual", valor=1, viabilidade=1, prioridade_manual=Decimal(30))
    fila = servico.fila()
    assert [i.id for i in fila] == [c.id, a.id, b.id]


def test_fila_desempata_por_criado_em_e_respeita_status_e_limite(banco, limpar):
    primeira = _qualificada(titulo="primeira", valor=2, viabilidade=2)
    segunda = _qualificada(titulo="segunda", valor=2, viabilidade=2)
    assert [i.id for i in servico.fila()] == [primeira.id, segunda.id]
    assert [i.id for i in servico.fila(limite=1)] == [primeira.id]
    # a ideia piloto está em triagem
    assert [i.id for i in servico.fila(status="triagem")] == [IDEIA_SEMENTE.id]


def test_fila_exclui_excluidas(banco, limpar):
    mantida = _qualificada(titulo="mantida")
    excluida = _qualificada(titulo="excluida", valor=5, viabilidade=5)
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute("update acervo.ideia set excluido_em = now() where id = %s", [excluida.id])
    assert [i.id for i in servico.fila()] == [mantida.id]


# --- transições ---------------------------------------------------------------------------
def test_arquivar_sem_motivo_409(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    with pytest.raises(RegraViolada):
        servico.transicionar(ideia.id, "arquivada", "carla")
    assert servico.obter(ideia.id).status == "rascunho"
    with pytest.raises(RegraViolada):
        servico.transicionar(ideia.id, "arquivada", "carla", motivo="   ")


def test_arquivar_com_motivo(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    arquivada = servico.transicionar(ideia.id, "arquivada", "carla", motivo="Fora de escopo")
    assert arquivada.status == "arquivada"
    assert arquivada.motivo_status == "Fora de escopo"


def test_transicao_invalida_pelo_trigger(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    with pytest.raises(RegraViolada, match="inválida"):
        servico.transicionar(ideia.id, "em_projeto", "ana")
    assert servico.obter(ideia.id).status == "rascunho"


def test_qualificar_sem_fonte_candidata_409(banco, limpar):
    ideia = servico.criar_ideia(_dados(status="triagem"), "ana")
    with pytest.raises(RegraViolada):
        servico.transicionar(ideia.id, "qualificada", "carla")


def test_transicao_sem_motivo_limpa_motivo_anterior(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    servico.transicionar(ideia.id, "arquivada", "carla", motivo="Duplicada de fato")
    reaberta = servico.transicionar(ideia.id, "triagem", "carla")
    assert reaberta.status == "triagem"
    assert reaberta.motivo_status is None


def test_transicao_ate_concluida(banco, limpar):
    ideia = _qualificada()
    assert servico.transicionar(ideia.id, "em_projeto", "sistema").status == "em_projeto"
    assert servico.transicionar(ideia.id, "concluida", "sistema").status == "concluida"


def test_transicionar_para_o_mesmo_status_e_rejeitado(banco, limpar):
    ideia = servico.criar_ideia(_dados(status="triagem"), "ana")
    with pytest.raises(RegraViolada, match="triagem"):
        servico.transicionar(ideia.id, "triagem", "carla")
    assert [e.para_status for e in servico.historico(ideia.id)] == ["triagem"]


def test_transicionar_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        servico.transicionar(uuid4(), "triagem", "ana")


def test_fundir_registra_relacao(banco, limpar):
    absorvida = servico.criar_ideia(_dados(titulo="absorvida"), "ana")
    destino = servico.criar_ideia(_dados(titulo="destino"), "ana")
    fundida = servico.transicionar(
        absorvida.id, "fundida", "carla", motivo="Mesma pergunta", fundida_em_id=destino.id
    )
    assert fundida.status == "fundida"
    assert fundida.fundida_em_id == destino.id
    assert fundida.motivo_status == "Mesma pergunta"
    assert _consultar(banco, "select ideia_id, relacionada_id, tipo from acervo.ideia_relacao") == [
        (absorvida.id, destino.id, "duplicada")
    ]


def test_fundir_exige_destino_valido(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    with pytest.raises(RegraViolada, match="fundida_em_id"):
        servico.transicionar(ideia.id, "fundida", "carla")
    with pytest.raises(RegraViolada):
        servico.transicionar(ideia.id, "fundida", "carla", fundida_em_id=ideia.id)
    with pytest.raises(RegraViolada):
        servico.transicionar(ideia.id, "fundida", "carla", fundida_em_id=uuid4())
    assert servico.obter(ideia.id).status == "rascunho"
    assert _consultar(banco, "select 1 from acervo.ideia_relacao") == []


def test_fundida_em_id_so_vale_para_fundida(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    outra = servico.criar_ideia(_dados(), "ana")
    with pytest.raises(RegraViolada, match="fundida_em_id"):
        servico.transicionar(ideia.id, "triagem", "ana", fundida_em_id=outra.id)


# --- derivada (retroalimentação) ----------------------------------------------------------
def test_criar_ideia_derivada_idempotente(banco, limpar):
    id_ = uuid4()
    primeira = servico.criar_ideia_derivada(id_, _dados(titulo="derivada"), "sistema")
    segunda = servico.criar_ideia_derivada(id_, _dados(titulo="outro título"), "sistema")
    assert primeira.id == segunda.id == id_
    assert segunda == primeira
    assert primeira.origem == "retroalimentacao"
    linhas = _consultar(banco, "select titulo from acervo.ideia where id = %s", [id_])
    assert linhas == [("derivada",)]
    eventos = _consultar(banco, "select 1 from acervo.ideia_evento where ideia_id = %s", [id_])
    assert len(eventos) == 1


def test_criar_ideia_derivada_forca_origem(banco, limpar):
    derivada = servico.criar_ideia_derivada(
        uuid4(), _dados(origem="fonte_externa", origem_url="https://x.org"), "sistema"
    )
    assert derivada.origem == "retroalimentacao"


# --- relações e histórico -----------------------------------------------------------------
def test_relacionar_e_idempotente(banco, limpar):
    a = servico.criar_ideia(_dados(), "ana")
    b = servico.criar_ideia(_dados(), "ana")
    servico.relacionar(a.id, b.id, "filha", "ana")
    servico.relacionar(a.id, b.id, "filha", "ana")
    assert _consultar(banco, "select ideia_id, relacionada_id, tipo from acervo.ideia_relacao") == [
        (a.id, b.id, "filha")
    ]


def test_relacionar_valida_ideias_e_tipo(banco, limpar):
    a = servico.criar_ideia(_dados(), "ana")
    with pytest.raises(NaoEncontrado):
        servico.relacionar(a.id, uuid4(), "filha", "ana")
    with pytest.raises(NaoEncontrado):
        servico.relacionar(uuid4(), a.id, "filha", "ana")
    with pytest.raises(RegraViolada):
        servico.relacionar(a.id, a.id, "filha", "ana")
    b = servico.criar_ideia(_dados(), "ana")
    with pytest.raises(RegraViolada):
        servico.relacionar(a.id, b.id, "inexistente", "ana")


def test_historico_registra_quem_mudou_o_que(banco, limpar):
    ideia = servico.criar_ideia(_dados(), "ana")
    servico.transicionar(ideia.id, "triagem", "ana")
    servico.transicionar(ideia.id, "arquivada", "carla", motivo="Sem dados")
    historico = servico.historico(ideia.id)
    assert all(isinstance(e, EventoDTO) for e in historico)
    assert [(e.evento, e.de_status, e.para_status, e.motivo, e.autor) for e in historico] == [
        ("criada", None, "rascunho", None, "ana"),
        ("status_alterado", "rascunho", "triagem", None, "ana"),
        ("status_alterado", "triagem", "arquivada", "Sem dados", "carla"),
    ]
    assert all(e.ideia_id == ideia.id for e in historico)


def test_historico_de_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        servico.historico(UUID(int=1))
