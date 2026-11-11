"""Construtores de projetos para os testes: especificação realista da semente + helpers de fluxo."""

from uuid import UUID, uuid4

import psycopg

from ais4.acervo import servico as acervo
from ais4.acervo.servico import IdeiaCriar
from ais4.auth import Usuario
from ais4.comum.dto import ProjetoDTO
from ais4.comum.espec import EspecProjeto, Objetivo, Risco
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from ais4.projetos import servico
from ais4.projetos.servico import CriarProjeto
from tests.playbooks.semente import IDEIA_SEMENTE

PLAYBOOK_ID = UUID("00000000-0000-4000-8000-000000000001")
GATES = {  # codigo do marco -> papel do revisor (playbook semente)
    "M1": "responsavel_por_dados",
    "M3": "revisor_metodologico",
    "M5": "dono_do_projeto",
    "M6": "curador_do_acervo",
}
RESPONSAVEIS = {
    "dono_do_projeto": "ana",
    "responsavel_por_dados": "davi",
    "revisor_metodologico": "rita",
    "curador_do_acervo": "carla",
}


def usuario(nome: str, *papeis: str) -> Usuario:
    return Usuario(nome=nome, papeis=frozenset(papeis))


def espec() -> EspecProjeto:
    """Especificação da semente (6 marcos M1..M6) com as seções vazias preenchidas."""
    base = ProjetoAcademicoSimples().instanciar(IDEIA_SEMENTE)
    return base.model_copy(
        update={
            "objetivos": [
                Objetivo(codigo="O1", descricao="Mapear a oferta", entregavel="relatorio")
            ],
            "riscos": [
                Risco(
                    categoria="dados",
                    descricao="Séries com quebra metodológica",
                    probabilidade=3,
                    impacto=4,
                    mitigacao="Harmonizar",
                )
            ],
        }
    )


def comando(**kw) -> CriarProjeto:
    campos = {
        "titulo": "Abastecimento 2050",
        "ideia_origem_id": IDEIA_SEMENTE.id,
        "playbook_id": PLAYBOOK_ID,
        "playbook_versao": "0.1.0",
        "dono": "ana",
        "especificacao": espec(),
        "aprovado_por": "ana",
        "responsaveis": dict(RESPONSAVEIS),
    }
    campos.update(kw)
    return CriarProjeto(**campos)


def novo_projeto(**kw) -> ProjetoDTO:
    return servico.criar_projeto(comando(**kw), "ana")


def levar_a_gate(projeto_id: UUID, codigo: str) -> None:
    """pendente -> em_andamento -> aguardando_gate."""
    servico.atualizar_marco(projeto_id, codigo, "em_andamento", "sistema")
    servico.atualizar_marco(projeto_id, codigo, "aguardando_gate", "sistema")


def aprovar_gate(projeto_id: UUID, codigo: str) -> None:
    levar_a_gate(projeto_id, codigo)
    pessoa = RESPONSAVEIS[GATES[codigo]]
    servico.decidir_gate(projeto_id, codigo, "aprovado", "ok", usuario(pessoa, GATES[codigo]))


def aprovar_todos_os_gates(projeto_id: UUID) -> None:
    for codigo in GATES:
        aprovar_gate(projeto_id, codigo)


def ir_para_revisao(projeto_id: UUID) -> None:
    servico.transicionar(projeto_id, "em_execucao", "ana")
    servico.transicionar(projeto_id, "em_revisao", "ana")


def registrar_retroalimentacao(projeto_id: UUID) -> None:
    """Uma lição e um próximo passo já com ideia gerada: o que falta para concluir."""
    servico.registrar_licao(projeto_id, "funcionou", "Harmonizar cedo", None)
    passo = servico.registrar_proximo_passo(projeto_id, "Estender a 2060")
    ideia = acervo.criar_ideia_derivada(
        uuid4(),
        IdeiaCriar(
            titulo="Estender a 2060",
            tipo="pergunta_de_pesquisa",
            descricao="d",
            pergunta_norteadora="p?",
            dominio=["agro"],
            valor=3,
            viabilidade=3,
        ),
        "sistema",
    )
    servico.vincular_ideia_gerada(passo, ideia.id)


def preparar_fechamento(projeto_id: UUID) -> None:
    """Gates aprovados, projeto em revisão, lição e próximo passo registrados."""
    aprovar_todos_os_gates(projeto_id)
    ir_para_revisao(projeto_id)
    registrar_retroalimentacao(projeto_id)


def consultar(banco: str, sql: str, params=()):
    with psycopg.connect(banco) as c:
        return c.execute(sql, params).fetchall()
