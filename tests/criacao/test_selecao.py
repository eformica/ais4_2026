import dataclasses
import threading
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import text

import ais4.playbooks  # noqa: F401  (registra o playbook embarcado)
from ais4.acervo import servico as acervo
from ais4.auth import Usuario
from ais4.comum.erros import RegraViolada
from ais4.criacao import motor
from ais4.db import sessao
from ais4.playbooks import registro
from ais4.playbooks import servico as playbooks
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from ais4.playbooks.base import Avaliacao
from ais4.projetos import servico as projetos
from tests.criacao.fabrica import consultar, ideia_qualificada, qualificar_semente
from tests.playbooks.semente import IDEIA_SEMENTE
from tests.projetos.fabrica import novo_projeto

GUARDIAO = Usuario("fred", frozenset({"guardiao_de_playbooks"}))
# tipo fora dos típicos do playbook embarcado: a versão 0.1.0 pontua 0.5 (e não 1.0)
IDEIA_MEIA_PONTUACAO = dataclasses.replace(IDEIA_SEMENTE, tipo="oportunidade")
SEMENTE_PLAYBOOK_ID = UUID("00000000-0000-4000-8000-000000000001")


# --- seleção e reserva -----------------------------------------------------------------------
def test_seleciona_a_ideia_de_maior_prioridade_e_reserva(banco, limpar):
    qualificar_semente()  # prioridade 12
    maior = ideia_qualificada("Prioritária", valor=5, viabilidade=5)  # 25

    ideia, proposta_id = motor.selecionar_e_reservar("motor-1")

    assert ideia.id == maior.id
    (linha,) = consultar(
        banco,
        "select ideia_id, status, solicitado_por, playbook_id from criacao.proposta where id = %s",
        (proposta_id,),
    )
    assert linha == (maior.id, "rascunho", "motor-1", None)


def test_ideia_reservada_e_pulada_na_chamada_seguinte(banco, limpar):
    qualificar_semente()
    maior = ideia_qualificada("Prioritária")

    primeira, _ = motor.selecionar_e_reservar("motor-1")
    segunda, _ = motor.selecionar_e_reservar("motor-2")

    assert primeira.id == maior.id
    assert segunda.id == IDEIA_SEMENTE.id
    assert motor.selecionar_e_reservar("motor-3") is None


def test_sem_ideia_qualificada_retorna_none(banco, limpar):
    assert motor.selecionar_e_reservar("motor-1") is None


def test_dois_motores_nao_pegam_a_mesma_ideia(banco, limpar):
    qualificar_semente()
    ideia_qualificada("Segunda ideia")
    largada = threading.Barrier(2)
    resultados: list = []
    erros: list[BaseException] = []

    def motor_concorrente(nome: str) -> None:
        try:
            largada.wait(timeout=10)
            resultados.append(motor.selecionar_e_reservar(nome))
        except BaseException as erro:  # noqa: BLE001 - repassado ao teste
            erros.append(erro)

    threads = [threading.Thread(target=motor_concorrente, args=(f"motor-{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert erros == []
    assert all(r is not None for r in resultados) and len(resultados) == 2
    assert len({ideia.id for ideia, _ in resultados}) == 2
    assert len({proposta_id for _, proposta_id in resultados}) == 2
    (ativas,) = consultar(banco, "select count(*) from criacao.proposta where status = 'rascunho'")
    assert ativas[0] == 2


def test_sem_capacidade_retorna_none(banco, limpar):
    qualificar_semente()
    projeto = novo_projeto()
    projetos.transicionar(projeto.id, "em_execucao", "ana")
    assert projetos.contar_por_status("em_execucao") == 1  # AIS4_MAX_PROJETOS_EM_EXECUCAO=1

    assert motor.selecionar_e_reservar("motor-1") is None
    assert consultar(banco, "select count(*) from criacao.proposta")[0][0] == 0


def test_projeto_aprovado_nao_ocupa_capacidade(banco, limpar):
    qualificar_semente()
    novo_projeto()  # status 'aprovado'
    assert motor.selecionar_e_reservar("motor-1") is not None


def test_contar_por_status_ignora_projetos_excluidos(banco, limpar):
    projeto = novo_projeto()
    projetos.transicionar(projeto.id, "em_execucao", "ana")
    assert projetos.contar_por_status("em_execucao") == 1
    assert projetos.contar_por_status("pausado") == 0
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute("update projetos.projeto set excluido_em = now()")
    assert projetos.contar_por_status("em_execucao") == 0


# --- esquema: R6 e unicidade ------------------------------------------------------------------
def test_proposta_ativa_sem_playbook_so_como_rascunho(banco, limpar):
    qualificar_semente()
    _, proposta_id = motor.selecionar_e_reservar("motor-1")
    with pytest.raises(RegraViolada), sessao("criacao", "ana") as s:
        s.execute(
            text("update criacao.proposta set status = 'aguardando_aprovacao' where id = :i"),
            {"i": proposta_id},
        )


def test_proposta_devolvida_exige_motivo(banco, limpar):
    qualificar_semente()
    _, proposta_id = motor.selecionar_e_reservar("motor-1")
    with pytest.raises(RegraViolada), sessao("criacao", "ana") as s:
        s.execute(
            text("update criacao.proposta set status = 'devolvida' where id = :i"),
            {"i": proposta_id},
        )


def test_iteracao_so_aceita_insercao(banco, limpar):
    qualificar_semente()
    _, proposta_id = motor.selecionar_e_reservar("motor-1")
    with sessao("criacao", "ana") as s:
        s.execute(
            text(
                "insert into criacao.proposta_iteracao (proposta_id, n, rascunho, "
                "aprovada_pela_critica) values (:i, 1, '{}', false)"
            ),
            {"i": proposta_id},
        )
    with pytest.raises(RegraViolada), sessao("criacao", "ana") as s:
        s.execute(text("update criacao.proposta_iteracao set lacunas = '[1]'"))


# --- casamento com o playbook -----------------------------------------------------------------
def test_casa_o_playbook_da_semente(banco, limpar):
    resultado = motor.casar_playbook(IDEIA_SEMENTE)

    assert resultado is not None
    playbook, avaliacao = resultado
    assert (playbook.id, playbook.versao) == ("projeto-academico-simples", "0.1.0")
    assert avaliacao.aplicavel is True


def test_sem_playbook_aplicavel_retorna_none(banco, limpar):
    sem_fontes = dataclasses.replace(IDEIA_SEMENTE, fontes_candidatas=[])
    assert motor.casar_playbook(sem_fontes) is None


def _variante(versao: str, pontuacao: float, aplicavel: bool = True):
    """Nova versão do playbook embarcado com uma avaliação fixa."""

    class Variante(ProjetoAcademicoSimples):
        def aplicavel(self, ideia):
            return Avaliacao(aplicavel, pontuacao, [])

    Variante.versao = versao
    return registro.registrar(Variante)


def _publicar(classe, status: str) -> None:
    pb = classe()
    playbooks.sincronizar(pb, "ana")
    versao = next(v for v in playbooks.listar_versoes() if v.versao == pb.versao)
    playbooks.alterar_status(versao.playbook_id, pb.versao, "experimental", GUARDIAO)
    if status == "validado":
        playbooks.alterar_status(versao.playbook_id, pb.versao, "validado", GUARDIAO)


def test_casa_a_versao_de_maior_pontuacao(banco, limpar):
    with registro.isolado():
        _publicar(_variante("0.2.0", 0.2), "experimental")
        _publicar(_variante("0.3.0", 0.9), "experimental")
        _publicar(_variante("0.4.0", 1.0, aplicavel=False), "experimental")

        playbook, avaliacao = motor.casar_playbook(IDEIA_MEIA_PONTUACAO)

    assert playbook.versao == "0.3.0"
    assert avaliacao.pontuacao == 0.9


def test_empate_prefere_validado_a_experimental(banco, limpar):
    with registro.isolado():
        _publicar(_variante("0.2.0", 0.9), "experimental")
        _publicar(_variante("0.3.0", 0.9), "validado")
        _publicar(_variante("0.4.0", 0.9), "experimental")

        playbook, _ = motor.casar_playbook(IDEIA_MEIA_PONTUACAO)

    assert playbook.versao == "0.3.0"


def test_versao_ativa_sem_classe_registrada_e_ignorada(banco, limpar):
    with registro.isolado():
        _publicar(_variante("0.2.0", 0.9), "experimental")
    # fora do isolado, a classe 0.2.0 saiu do registro, mas a versão segue ativa no banco

    playbook, _ = motor.casar_playbook(IDEIA_SEMENTE)

    assert playbook.versao == "0.1.0"


def test_vincular_playbook_grava_playbook_e_versao_na_proposta(banco, limpar):
    qualificar_semente()
    ideia, proposta_id = motor.selecionar_e_reservar("motor-1")
    playbook, _ = motor.casar_playbook(ideia)

    motor.vincular_playbook(proposta_id, playbook, "motor-1")

    (linha,) = consultar(
        banco, "select playbook_id, playbook_versao from criacao.proposta where id = %s",
        (proposta_id,),
    )  # fmt: skip
    assert linha == (SEMENTE_PLAYBOOK_ID, "0.1.0")


# --- devolução sem playbook -------------------------------------------------------------------
def test_sem_playbook_devolve_e_registra_lacuna(banco, limpar):
    qualificar_semente()
    ideia, proposta_id = motor.selecionar_e_reservar("motor-1")

    motor.devolver_sem_playbook(
        proposta_id, ideia, ["Nenhum playbook cobre séries temporais", "Falta pré-requisito"],
        "motor-1",
    )  # fmt: skip

    (proposta,) = consultar(
        banco, "select status, motivo from criacao.proposta where id = %s", (proposta_id,)
    )
    assert proposta[0] == "devolvida"
    assert "Nenhum playbook cobre séries temporais" in proposta[1]

    (lacuna,) = acervo.lacunas("aberta")
    assert lacuna.ideia_id == ideia.id
    assert "Nenhum playbook cobre séries temporais" in lacuna.descricao
    assert "Falta pré-requisito" in lacuna.descricao

    atual = acervo.obter(ideia.id)
    assert atual.status == "triagem"
    assert "Nenhum playbook cobre séries temporais" in atual.motivo_status


def test_ideia_devolvida_pode_ser_selecionada_de_novo_depois_de_requalificada(banco, limpar):
    qualificar_semente()
    ideia, proposta_id = motor.selecionar_e_reservar("motor-1")
    motor.devolver_sem_playbook(proposta_id, ideia, ["sem playbook"], "motor-1")
    acervo.transicionar(ideia.id, "qualificada", "ana")

    de_novo = motor.selecionar_e_reservar("motor-1")

    assert de_novo is not None and de_novo[0].id == ideia.id
