import pytest

from ais4.comum.espec import (
    Adaptacao,
    Escopo,
    EspecProjeto,
    Fonte,
    Objetivo,
    Restricao,
    ResultadoEsperado,
    Risco,
)
from ais4.criacao.validacao import Lacuna, validar_espec
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from tests.criacao.fabrica import espec_completa
from tests.playbooks.semente import IDEIA_SEMENTE

ETAPAS = list(ProjetoAcademicoSimples.etapas)
ESPEC_OK = espec_completa(IDEIA_SEMENTE)


def _objetivos(n: int) -> list[Objetivo]:
    return [Objetivo(codigo=f"O{i}", descricao="d", entregavel="relatório") for i in range(n)]


def _sem_marco(codigo_etapa: str):
    return lambda e: e.model_copy(
        update={"cronograma": [m for m in e.cronograma if m.etapa_codigo != codigo_etapa]}
    )


def _marco_de(codigo_etapa: str, **mudancas):
    def mutar(e):
        cronograma = [
            m.model_copy(update=mudancas) if m.etapa_codigo == codigo_etapa else m
            for m in e.cronograma
        ]
        return e.model_copy(update={"cronograma": cronograma})

    return mutar


def _com(campo: str, valor):
    return lambda e: e.model_copy(update={campo: valor})


def _risco(categoria: str) -> Risco:
    return Risco(categoria=categoria, descricao="r", probabilidade=2, impacto=2, mitigacao="m")


def test_validar_espec_completa_sem_lacunas():
    assert validar_espec(ESPEC_OK, ETAPAS) == []


@pytest.mark.parametrize(
    ("mutacao", "secao"),
    [
        pytest.param(_com("objetivos", _objetivos(2)), "objetivos", id="2-objetivos"),
        pytest.param(_com("objetivos", _objetivos(8)), "objetivos", id="8-objetivos"),
        pytest.param(
            _com(
                "objetivos",
                [*_objetivos(2), Objetivo(codigo="O9", descricao="d", entregavel="  ")],
            ),
            "objetivos",
            id="objetivo-sem-entregavel",
        ),
        pytest.param(_com("escopo", Escopo(dentro=["a"], fora=[])), "escopo", id="sem-fora"),
        pytest.param(
            _com("restricoes", [Restricao(tipo="prazo", descricao="d", metrica="")]),
            "restricoes",
            id="restricao-sem-metrica",
        ),
        pytest.param(
            _com(
                "metodologia",
                ESPEC_OK.metodologia.model_copy(
                    update={"adaptacoes": [Adaptacao(descricao="x", justificativa=" ")]}
                ),
            ),
            "metodologia",
            id="adaptacao-sem-justificativa",
        ),
        pytest.param(
            _com("fontes_dados", [Fonte(nome="A", status_acesso="inacessivel")]),
            "fontes_dados",
            id="nenhuma-fonte-acessivel",
        ),
        pytest.param(_com("fontes_dados", []), "fontes_dados", id="sem-fontes"),
        pytest.param(
            _com("resultados_esperados", [ResultadoEsperado(tipo="relatorio", formato="pdf")]),
            "resultados_esperados",
            id="resultado-sem-destino",
        ),
        pytest.param(
            _com("resultados_esperados", [ResultadoEsperado(tipo="relatorio", destino="repo")]),
            "resultados_esperados",
            id="resultado-sem-formato",
        ),
        pytest.param(_com("riscos", [_risco("dados"), _risco("prazo")]), "riscos", id="sem-metodo"),
        pytest.param(_com("riscos", [_risco("metodo")]), "riscos", id="sem-dados"),
        pytest.param(_sem_marco("modelagem"), "cronograma", id="gate-de-modelagem-sem-marco"),
        pytest.param(
            _marco_de("modelagem", exige_gate=False, papel_revisor=None),
            "cronograma",
            id="marco-sem-gate-para-etapa-com-gate",
        ),
        pytest.param(
            _marco_de("modelagem", papel_revisor="dono_do_projeto"),
            "cronograma",
            id="marco-com-outro-papel-revisor",
        ),
        pytest.param(
            _marco_de("analise_exploratoria", responsavel=""), "cronograma", id="sem-resp"
        ),
    ],
)
def test_validar_espec_detecta(mutacao, secao):
    lacunas = validar_espec(mutacao(ESPEC_OK), ETAPAS)
    assert len(lacunas) == 1
    assert isinstance(lacunas[0], Lacuna)
    assert lacunas[0].secao == secao
    assert lacunas[0].mensagem


def test_validar_espec_acumula_lacunas_de_secoes_diferentes():
    ruim = ESPEC_OK.model_copy(
        update={"objetivos": _objetivos(1), "escopo": Escopo(), "fontes_dados": []}
    )
    secoes = {lacuna.secao for lacuna in validar_espec(ruim, ETAPAS)}
    assert secoes == {"objetivos", "escopo", "fontes_dados"}


def test_validar_espec_vazia_aponta_todas_as_secoes_checaveis():
    secoes = {lacuna.secao for lacuna in validar_espec(EspecProjeto(), ETAPAS)}
    assert secoes == {"objetivos", "escopo", "fontes_dados", "riscos", "cronograma"}
