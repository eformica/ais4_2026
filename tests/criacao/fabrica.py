"""Construtores compartilhados pelos testes do Motor de Criação."""

import json

import psycopg

from ais4.acervo import servico as acervo
from ais4.acervo.servico import IdeiaAtualizar, IdeiaCriar
from ais4.comum.dto import IdeiaDTO
from ais4.comum.espec import (
    Escopo,
    EspecProjeto,
    Fonte,
    Objetivo,
    Restricao,
    ResultadoEsperado,
    Risco,
)
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from tests.playbooks.semente import IDEIA_SEMENTE

CENSO = "IBGE - Censo 2022"
URL_CENSO = "https://www.ibge.gov.br/censo"


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


def espec_completa(ideia: IdeiaDTO) -> EspecProjeto:
    """Especificação que cumpre todos os critérios de 4.1 checáveis sem LLM (esqueleto do playbook
    da semente + o que o redator preenche)."""
    base = ProjetoAcademicoSimples().instanciar(ideia)
    return base.model_copy(
        update={
            "objetivos": [
                Objetivo(codigo=f"O{i}", descricao="Objetivo", entregavel="relatório")
                for i in (1, 2, 3)
            ],
            "escopo": Escopo(
                dentro=["Brasil, 2020-2050"],
                fora=["Previsão de preços"],
                recorte_temporal="2020-2050",
                recorte_geografico="Brasil",
                recorte_tematico="alimentos",
            ),
            "restricoes": [Restricao(tipo="prazo", descricao="6 meses", metrica="data final")],
            "fontes_dados": [
                Fonte(
                    nome=CENSO,
                    url="https://www.ibge.gov.br/censo",
                    cobertura="2022",
                    periodicidade="decenal",
                    licenca="CC BY 4.0",
                    status_acesso="acessivel",
                )
            ],
            "resultados_esperados": [
                ResultadoEsperado(tipo="relatorio", formato="pdf", destino="repositório")
            ],
            "riscos": [
                Risco(
                    categoria=categoria,
                    descricao=f"Risco de {categoria}",
                    probabilidade=3,
                    impacto=3,
                    mitigacao="Plano de mitigação",
                )
                for categoria in ("dados", "metodo")
            ],
        }
    )


def verificacao_acessivel(nome: str = CENSO, url: str = URL_CENSO):
    """Roteiro do verificador de fontes: registra a fonte como acessível e responde."""

    def roteiro(ferramentas):
        ferramentas["registrar_verificacao"](
            nome=nome,
            url=url,
            status="acessivel",
            cobertura=json.dumps({"temporal": "2022", "licenca": "CC BY 4.0"}),
            evidencia="web_fetch abriu a página.",
        )
        return "registrado"

    return roteiro


def qualificar_semente_com_uma_fonte() -> IdeiaDTO:
    """A semente com uma só fonte candidata (um roteiro de verificação basta) e `qualificada`."""
    acervo.atualizar(IDEIA_SEMENTE.id, IdeiaAtualizar(fontes_candidatas=[{"nome": CENSO}]), "ana")
    return qualificar_semente()
