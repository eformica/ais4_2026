from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ais4.comum.dag import validar_dag
from ais4.comum.dto import IdeiaDTO
from ais4.comum.erros import NaoEncontrado, RegraViolada
from ais4.comum.espec import EspecProjeto, Risco
from ais4.playbooks import registro
from ais4.playbooks.base import Etapa, PapelAgente, Playbook
from tests.playbooks.test_servico import PlaybookDeTeste


def _etapa(codigo, depende_de=()):
    return Etapa(codigo, 1, codigo, None, tuple(depende_de), PapelAgente("p", ""), (), (), "ok")


def _ideia(**kw):
    agora = datetime.now(UTC)
    campos = {
        "id": uuid4(),
        "titulo": "t",
        "tipo": "problema",
        "descricao": "d",
        "pergunta_norteadora": "p?",
        "dominio": ["agro", "demografia"],
        "origem": "humana",
        "origem_url": None,
        "playbook_sugerido_id": None,
        "fontes_candidatas": [],
        "restricoes": None,
        "valor": 3,
        "viabilidade": 3,
        "prioridade_manual": None,
        "prioridade": Decimal(9),
        "status": "triagem",
        "motivo_status": None,
        "fundida_em_id": None,
        "criado_por": "x",
        "criado_em": agora,
        "atualizado_em": agora,
    }
    campos.update(kw)
    return IdeiaDTO(**campos)


def test_playbook_abstrato_nao_instancia():
    with pytest.raises(TypeError):
        Playbook()


def test_retroalimentar_padrao_gera_uma_ideia_por_passo():
    ideia = _ideia()
    novas = PlaybookDeTeste().retroalimentar(["Estender para 2060", "x" * 150], ideia)
    assert len(novas) == 2
    assert novas[0].titulo == "Estender para 2060"
    assert novas[0].descricao == "Estender para 2060"
    assert novas[0].dominio == ["agro", "demografia"]
    assert novas[0].dominio is not ideia.dominio
    assert (novas[0].valor, novas[0].viabilidade) == (3, 3)
    assert len(novas[1].titulo) == 100
    assert PlaybookDeTeste().retroalimentar([], ideia) == []


def test_registro_registra_obtem_e_isola():
    with registro.isolado(vazio=True):
        assert registro.todos() == []
        assert registro.registrar(PlaybookDeTeste) is PlaybookDeTeste
        assert isinstance(registro.obter("playbook-de-teste", "0.1.0"), PlaybookDeTeste)
        assert len(registro.todos()) == 1
        with pytest.raises(ValueError, match="já registrado"):
            registro.registrar(PlaybookDeTeste)
        with pytest.raises(NaoEncontrado):
            registro.obter("playbook-de-teste", "9.9.9")
    with pytest.raises(NaoEncontrado):
        registro.obter("playbook-de-teste", "0.1.0")


def test_dag_aceita_grafo_aciclico_e_ignora_dependencia_inexistente():
    validar_dag([_etapa("a"), _etapa("b", ["a"]), _etapa("c", ["a", "b"]), _etapa("d", ["zzz"])])
    validar_dag([])


def test_dag_informa_o_ciclo():
    with pytest.raises(RegraViolada) as exc:
        validar_dag([_etapa("a", ["c"]), _etapa("b", ["a"]), _etapa("c", ["b"])])
    assert "a -> c -> b -> a" in exc.value.mensagem


def test_dag_ciclo_nao_alcancavel_da_primeira_etapa():
    with pytest.raises(RegraViolada, match="x -> y -> x"):
        validar_dag([_etapa("a"), _etapa("x", ["y"]), _etapa("y", ["x"])])


def test_espec_vazia_tem_exatamente_as_9_chaves():
    dump = EspecProjeto().model_dump(mode="json")
    assert set(dump) == {
        "definicao_problema",
        "escopo",
        "objetivos",
        "restricoes",
        "metodologia",
        "fontes_dados",
        "resultados_esperados",
        "riscos",
        "cronograma",
    }
    assert dump["objetivos"] == [] and dump["riscos"] == []


def test_espec_completa_serializa_em_json():
    espec = EspecProjeto.model_validate(
        {
            "objetivos": [{"codigo": "O1", "descricao": "d", "entregavel": "relatorio"}],
            "fontes_dados": [{"nome": "IBGE"}],
            "riscos": [{"categoria": "dados", "descricao": "d", "probabilidade": 5, "impacto": 1}],
            "cronograma": [
                {
                    "codigo": "M1",
                    "nome": "Dados",
                    "criterio_saida": "ok",
                    "responsavel": "humano:dono_do_projeto",
                    "data_planejada": "2026-12-01",
                }
            ],
        }
    )
    dump = espec.model_dump(mode="json")
    assert dump["fontes_dados"][0] == {
        "nome": "IBGE",
        "url": None,
        "cobertura": "",
        "periodicidade": "",
        "licenca": "",
        "status_acesso": "nao_testado",
    }
    assert dump["cronograma"][0]["data_planejada"] == "2026-12-01"


@pytest.mark.parametrize("valor", [0, 6])
def test_risco_probabilidade_e_impacto_entre_1_e_5(valor):
    with pytest.raises(ValidationError):
        Risco(categoria="dados", descricao="d", probabilidade=valor, impacto=3)
    with pytest.raises(ValidationError):
        Risco(categoria="dados", descricao="d", probabilidade=3, impacto=valor)


def test_enums_e_responsavel_invalidos_rejeitados():
    with pytest.raises(ValidationError):
        EspecProjeto.model_validate({"restricoes": [{"tipo": "inexistente", "descricao": "d"}]})
    with pytest.raises(ValidationError):
        EspecProjeto.model_validate(
            {
                "cronograma": [
                    {"codigo": "M", "nome": "n", "criterio_saida": "c", "responsavel": "x"}
                ]
            }
        )


def test_espec_listas_nao_sao_compartilhadas_entre_instancias():
    a, b = EspecProjeto(), EspecProjeto()
    a.objetivos.append({"codigo": "O1", "descricao": "d", "entregavel": "e"})  # type: ignore[arg-type]
    assert b.objetivos == []
