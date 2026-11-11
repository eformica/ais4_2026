"""Construtores compartilhados pelos testes do Motor de Criação."""

import psycopg

from ais4.acervo import servico as acervo
from ais4.acervo.servico import IdeiaCriar
from ais4.comum.dto import IdeiaDTO
from tests.playbooks.semente import IDEIA_SEMENTE


def consultar(banco, sql, params=()):
    with psycopg.connect(banco) as c:
        return c.execute(sql, params).fetchall()


def qualificar_semente() -> IdeiaDTO:
    """A ideia da semente (triagem, com fontes candidatas) passa a `qualificada`."""
    return acervo.transicionar(IDEIA_SEMENTE.id, "qualificada", "ana")


def ideia_qualificada(titulo="Outra ideia", valor=5, viabilidade=5, fontes=None) -> IdeiaDTO:
    """Ideia nova levada a `qualificada` pelo serviço (a rota HTTP proíbe; o serviço permite)."""
    ideia = acervo.criar_ideia(
        IdeiaCriar(
            titulo=titulo,
            tipo="pergunta_de_pesquisa",
            descricao="Contexto da ideia.",
            pergunta_norteadora="Qual é a pergunta que esta ideia pretende responder?",
            dominio=["agro"],
            valor=valor,
            viabilidade=viabilidade,
            fontes_candidatas=[{"nome": "IBGE - Censo 2022"}] if fontes is None else fontes,
            status="triagem",
        ),
        "ana",
    )
    return acervo.transicionar(ideia.id, "qualificada", "ana")
