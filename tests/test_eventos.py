import logging
import threading
import time
from uuid import UUID

import psycopg
import pytest

from ais4.acervo import handlers as acervo_handlers
from ais4.acervo import servico as acervo
from ais4.auth import Usuario
from ais4.comum import eventos
from ais4.comum.erros import NaoEncontrado
from ais4.comum.eventos import Evento
from ais4.playbooks import servico as playbooks
from ais4.projetos import servico as projetos
from tests.playbooks.semente import IDEIA_SEMENTE
from tests.projetos.fabrica import PLAYBOOK_ID, consultar, novo_projeto, preparar_fechamento

IDEIA = IDEIA_SEMENTE.id
CURADORA = Usuario("carla", frozenset({"curador_do_acervo"}))
GUARDIAO = Usuario("fred", frozenset({"guardiao_de_playbooks"}))


@pytest.fixture
def handlers(limpar):
    """Registro de handlers só com os do acervo; o registro global é restaurado ao final."""
    with eventos.isolado(vazio=True):
        acervo_handlers.registrar()
        yield


def _status_ideia() -> str:
    return acervo.obter(IDEIA).status


def _pendentes(banco: str, schema: str) -> int:
    return consultar(banco, f"select count(*) from {schema}.outbox where publicado_em is null")[0][
        0
    ]


def _projeto_com_ideia_qualificada():
    acervo.transicionar(IDEIA, "qualificada", "ana")
    return novo_projeto()


def _devolver_a_fila(banco: str, schema: str) -> None:
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute(f"update {schema}.outbox set publicado_em = null")


def _evento(banco: str, schema: str, tipo: str, para: str | None = None) -> Evento:
    """O último evento real do outbox com esse tipo (e, se dado, esse `para` no payload)."""
    linhas = consultar(
        banco,
        f"select id, agregado_id, tipo_evento, payload from {schema}.outbox "
        "where tipo_evento = %s order by id desc",
        (tipo,),
    )
    id_, agregado, tipo, payload = next(x for x in linhas if para is None or x[3]["para"] == para)
    return Evento(schema, id_, agregado, tipo, payload)


# ---------------------------------------------------------------------------
# Handlers do acervo
# ---------------------------------------------------------------------------
def test_projeto_criado_move_ideia_para_em_projeto(handlers, banco):
    projeto = _projeto_com_ideia_qualificada()
    assert _status_ideia() == "qualificada"  # nada acontece antes de publicar

    assert eventos.publicar_pendentes("projetos") == 1

    ideia = acervo.obter(IDEIA)
    assert ideia.status == "em_projeto"
    assert ideia.motivo_status == f"projeto {projeto.id} criado"
    assert _pendentes(banco, "projetos") == 0
    autor = consultar(
        banco, "select autor from acervo.ideia_evento where para_status = 'em_projeto'"
    )
    assert autor == [("sistema:outbox",)]


def test_reprocessar_evento_nao_falha(handlers, banco, caplog):
    _projeto_com_ideia_qualificada()
    assert eventos.publicar_pendentes("projetos") == 1
    antes = acervo.historico(IDEIA)

    # devolve o evento à fila: o handler roda de novo sobre a ideia já em em_projeto
    _devolver_a_fila(banco, "projetos")
    with caplog.at_level(logging.WARNING):
        assert eventos.publicar_pendentes("projetos") == 1

    assert _status_ideia() == "em_projeto"
    assert acervo.historico(IDEIA) == antes  # nenhuma transição nova
    assert not caplog.records  # já no estado alvo: no-op silencioso

    # chamar o handler diretamente, de novo, também é inofensivo
    acervo_handlers.ao_projeto_criado(_evento(banco, "projetos", "ProjetoCriado"))
    assert _status_ideia() == "em_projeto"


def test_cancelamento_devolve_ideia_para_qualificada(handlers, banco):
    projeto = _projeto_com_ideia_qualificada()
    assert eventos.publicar_pendentes("projetos") == 1
    assert _status_ideia() == "em_projeto"

    projetos.transicionar(projeto.id, "cancelado", "ana", "sem dados")
    assert eventos.publicar_pendentes("projetos") == 1

    ideia = acervo.obter(IDEIA)
    assert ideia.status == "qualificada"
    assert ideia.motivo_status == f"projeto {projeto.id} cancelado"

    # reprocessar o cancelamento não falha nem altera nada
    evento = _evento(banco, "projetos", "ProjetoStatusAlterado", "cancelado")
    acervo_handlers.ao_projeto_cancelado(evento)
    assert _status_ideia() == "qualificada"


def test_conclusao_move_ideia_para_concluida(handlers, banco):
    projeto = _projeto_com_ideia_qualificada()
    assert eventos.publicar_pendentes("projetos") == 1
    preparar_fechamento(projeto.id)
    projetos.transicionar(projeto.id, "concluido", "ana")

    eventos.publicar_pendentes("projetos")

    ideia = acervo.obter(IDEIA)
    assert ideia.status == "concluida"
    assert ideia.motivo_status == f"projeto {projeto.id} concluído"
    assert _pendentes(banco, "projetos") == 0


def test_estado_inesperado_da_ideia_avisa_e_nao_trava_o_outbox(handlers, banco, caplog):
    projeto = _projeto_com_ideia_qualificada()
    assert eventos.publicar_pendentes("projetos") == 1
    acervo.transicionar(IDEIA, "arquivada", "ana", "fora de escopo")
    projetos.transicionar(projeto.id, "cancelado", "ana", "sem dados")
    evento = _evento(banco, "projetos", "ProjetoStatusAlterado", "cancelado")

    with caplog.at_level(logging.WARNING, logger="ais4.acervo.handlers"):
        assert eventos.publicar_pendentes("projetos") == 1

    assert _status_ideia() == "arquivada"  # nada foi forçado
    assert _pendentes(banco, "projetos") == 0  # e o evento não ficou preso
    (aviso,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "arquivada" in aviso.getMessage() and str(evento.id) in aviso.getMessage()


def test_outros_status_de_projeto_nao_tocam_a_ideia(handlers, banco):
    projeto = _projeto_com_ideia_qualificada()
    eventos.publicar_pendentes("projetos")
    projetos.transicionar(projeto.id, "em_execucao", "ana")

    assert eventos.publicar_pendentes("projetos") == 1  # publicado: ninguém se interessa

    assert _status_ideia() == "em_projeto"


def test_playbook_validado_reabre_lacuna(handlers):
    compativel = acervo.registrar_lacuna(
        IDEIA, "falta análise exploratória", ["analise_exploratoria", "outra"], CURADORA
    )
    incompativel = acervo.registrar_lacuna(IDEIA, "falta algo", ["classe_inexistente"], CURADORA)

    # o evento da semente (proposto -> experimental) não reabre nada
    assert eventos.publicar_pendentes("playbooks") == 1
    assert {x.id for x in acervo.lacunas("aberta")} == {compativel, incompativel}

    playbooks.alterar_status(PLAYBOOK_ID, "0.1.0", "validado", GUARDIAO)
    assert eventos.publicar_pendentes("playbooks") == 1

    assert [x.id for x in acervo.lacunas("reabrir")] == [compativel]
    assert [x.id for x in acervo.lacunas("aberta")] == [incompativel]


def test_playbook_validado_reprocessado_nao_falha(handlers, banco):
    lacuna = acervo.registrar_lacuna(IDEIA, "d", ["analise_descritiva"], CURADORA)
    playbooks.alterar_status(PLAYBOOK_ID, "0.1.0", "validado", GUARDIAO)
    eventos.publicar_pendentes("playbooks")

    _devolver_a_fila(banco, "playbooks")
    assert eventos.publicar_pendentes("playbooks") == 2

    assert [x.id for x in acervo.lacunas("reabrir")] == [lacuna]


# ---------------------------------------------------------------------------
# Publicador
# ---------------------------------------------------------------------------
def test_handler_com_erro_nao_marca_publicado(limpar, banco, caplog):
    projeto = _projeto_com_ideia_qualificada()
    projetos.transicionar(projeto.id, "cancelado", "ana", "sem dados")  # 2º evento na fila
    chamadas: list[int] = []

    def quebrado(evento: Evento) -> None:
        chamadas.append(evento.id)
        raise RuntimeError("falha de teste")

    with eventos.isolado(vazio=True):
        eventos.assinar("projetos", "ProjetoCriado", quebrado)
        with caplog.at_level(logging.ERROR, logger="ais4.comum.eventos"):
            assert eventos.publicar_pendentes("projetos") == 0

    primeiro = _evento(banco, "projetos", "ProjetoCriado")
    assert chamadas == [primeiro.id]  # parou no primeiro erro; o 2º evento nem foi tentado
    assert _pendentes(banco, "projetos") == 2
    (erro,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "projetos" in erro.getMessage() and str(primeiro.id) in erro.getMessage()
    assert erro.exc_info is not None

    # com o handler certo, a próxima volta processa tudo, na ordem
    with eventos.isolado(vazio=True):
        acervo_handlers.registrar()
        assert eventos.publicar_pendentes("projetos") == 2
    assert _pendentes(banco, "projetos") == 0
    assert _status_ideia() == "qualificada"  # em_projeto e de volta


def test_falha_no_meio_do_lote_preserva_os_anteriores(limpar, banco):
    projeto = _projeto_com_ideia_qualificada()
    projetos.transicionar(projeto.id, "cancelado", "ana", "sem dados")  # eventos: criado, cancelado

    def quebra_no_cancelamento(evento: Evento) -> None:
        raise RuntimeError("falha de teste")

    with eventos.isolado(vazio=True):
        eventos.assinar(
            "projetos",
            "ProjetoStatusAlterado",
            quebra_no_cancelamento,
            lambda e: e.payload["para"] == "cancelado",
        )
        acervo_handlers.registrar()
        assert eventos.publicar_pendentes("projetos") == 1

    assert _pendentes(banco, "projetos") == 1
    assert _status_ideia() == "em_projeto"  # o primeiro evento foi aplicado e marcado


def test_evento_sem_assinante_e_marcado_como_publicado(limpar, banco):
    _projeto_com_ideia_qualificada()
    with eventos.isolado(vazio=True):
        assert eventos.publicar_pendentes("projetos") == 1
    assert _pendentes(banco, "projetos") == 0
    assert _status_ideia() == "qualificada"


def test_publicar_respeita_o_lote(limpar, banco):
    projeto = _projeto_com_ideia_qualificada()
    projetos.transicionar(projeto.id, "cancelado", "ana", "sem dados")  # 2 eventos
    with eventos.isolado(vazio=True):
        assert eventos.publicar_pendentes("projetos", lote=1) == 1
        assert _pendentes(banco, "projetos") == 1
        assert eventos.publicar_pendentes("projetos", lote=1) == 1
        assert eventos.publicar_pendentes("projetos", lote=1) == 0


def test_evento_entregue_com_dados_do_outbox(limpar):
    projeto = _projeto_com_ideia_qualificada()
    recebidos: list[Evento] = []
    with eventos.isolado(vazio=True):
        eventos.assinar("projetos", "ProjetoCriado", recebidos.append)
        eventos.assinar("projetos", "OutroTipo", recebidos.append)
        eventos.publicar_pendentes("projetos")

    (e,) = recebidos
    assert e.schema == "projetos" and e.tipo == "ProjetoCriado"
    assert isinstance(e.id, int) and e.agregado_id == projeto.id
    assert e.payload["ideia_origem_id"] == str(IDEIA)
    assert e.payload["para"] == "aprovado"


def test_filtro_decide_se_o_handler_roda(limpar, banco):
    _projeto_com_ideia_qualificada()
    chamado: list[int] = []
    with eventos.isolado(vazio=True):
        eventos.assinar("projetos", "ProjetoCriado", lambda e: chamado.append(1), lambda e: False)
        assert eventos.publicar_pendentes("projetos") == 1
    assert chamado == []
    assert _pendentes(banco, "projetos") == 0


def test_isolado_restaura_o_registro():
    antes = list(eventos._assinaturas)
    with eventos.isolado():
        eventos.assinar("projetos", "X", lambda e: None)
        assert len(eventos._assinaturas) == len(antes) + 1
    assert eventos._assinaturas == antes

    with eventos.isolado(vazio=True):
        assert eventos._assinaturas == []
    assert eventos._assinaturas == antes


def test_registrar_e_idempotente():
    with eventos.isolado(vazio=True):
        acervo_handlers.registrar()
        quantos = len(eventos._assinaturas)
        acervo_handlers.registrar()
        assert len(eventos._assinaturas) == quantos == 4


def test_carregar_handlers_registra_o_acervo_e_a_execucao():
    with eventos.isolado(vazio=True):
        eventos.carregar_handlers()
        eventos.carregar_handlers()
        assert len(eventos._assinaturas) == 7  # 4 do acervo + 2 da execução + 1 do 2.2
        assert {a.schema for a in eventos._assinaturas} == {"projetos", "playbooks"}


def test_loop_publica_e_ignora_schemas_inexistentes(handlers, banco):
    # criacao e execucao ainda não têm schema: o loop os ignora em vez de quebrar
    projeto = _projeto_com_ideia_qualificada()
    parar = threading.Event()
    fio = threading.Thread(target=eventos.loop, args=(0.05, parar), daemon=True)
    fio.start()
    limite = time.monotonic() + 20

    def esperar(status: str) -> None:
        while _status_ideia() != status and time.monotonic() < limite:
            time.sleep(0.05)
        assert _status_ideia() == status

    try:
        esperar("em_projeto")
        projetos.transicionar(projeto.id, "cancelado", "ana", "sem dados")
        esperar("qualificada")
    finally:
        parar.set()
        fio.join(timeout=10)
    assert not fio.is_alive()
    assert _pendentes(banco, "projetos") == 0


def test_loop_ja_parado_retorna_de_imediato():
    parar = threading.Event()
    parar.set()
    eventos.loop(0.01, parar)


# ---------------------------------------------------------------------------
# Serviços de apoio aos handlers
# ---------------------------------------------------------------------------
def test_classe_de_problema_do_playbook(limpar):
    assert playbooks.classe_de_problema(PLAYBOOK_ID) == [
        "analise_exploratoria",
        "analise_descritiva",
        "projecao_por_cenarios",
    ]


def test_classe_de_problema_de_playbook_inexistente(limpar):
    with pytest.raises(NaoEncontrado):
        playbooks.classe_de_problema(UUID(int=1))


def test_marcar_lacunas_para_reabrir(limpar):
    a = acervo.registrar_lacuna(IDEIA, "a", ["x", "y"], CURADORA)
    b = acervo.registrar_lacuna(IDEIA, "b", ["z"], CURADORA)
    c = acervo.registrar_lacuna(IDEIA, "c", ["x"], CURADORA)
    desconhecida = acervo.registrar_lacuna(IDEIA, "d", [], CURADORA)

    # sem classes em comum, só a lacuna de classe desconhecida (vazia) reabre
    assert acervo.marcar_lacunas_para_reabrir(["w"], "sistema:outbox") == [desconhecida]
    assert set(acervo.marcar_lacunas_para_reabrir(["x"], "sistema:outbox")) == {a, c}
    assert acervo.marcar_lacunas_para_reabrir(["x"], "sistema:outbox") == []  # já reabertas
    assert {x.id for x in acervo.lacunas("reabrir")} == {a, c, desconhecida}
    assert [x.id for x in acervo.lacunas("aberta")] == [b]
