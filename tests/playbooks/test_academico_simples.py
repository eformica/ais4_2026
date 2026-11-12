import json
from dataclasses import replace

import pytest

from ais4.comum.dto import ArtefatoRef
from ais4.comum.espec import EspecProjeto
from ais4.playbooks import registro
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from ais4.playbooks.base import SaidaEtapa
from ais4.playbooks.servico import sincronizar
from tests.playbooks.semente import IDEIA_SEMENTE

PB = ProjetoAcademicoSimples()
REF = ArtefatoRef("file:///nao-existe", "0" * 64, "x", "json")


def etapa(codigo):
    return next(e for e in PB.etapas if e.codigo == codigo)


def saida_com(codigo, **sobrescritas):
    """Saída com todos os artefatos da etapa apontando para um arquivo inexistente."""
    artefatos = {nome: REF for nome in etapa(codigo).saidas}
    artefatos.update(sobrescritas)
    return SaidaEtapa(artefatos, "")


def ref_json(tmp_path, nome, conteudo):
    caminho = tmp_path / f"{nome}.json"
    caminho.write_text(
        conteudo if isinstance(conteudo, str) else json.dumps(conteudo), encoding="utf-8"
    )
    return ArtefatoRef(caminho.as_uri(), "0" * 64, caminho.name, "json")


# --- registro e semente -------------------------------------------------------------------


def test_classe_bate_com_semente(banco, limpar):
    sincronizar(ProjetoAcademicoSimples(), "teste")  # não levanta: 0.1.0 já existe e coincide


def test_importar_o_pacote_registra_o_playbook():
    assert isinstance(registro.obter("projeto-academico-simples", "0.1.0"), ProjetoAcademicoSimples)


def test_prompt_base_cobre_saidas_e_conclusao_de_cada_etapa():
    for e in PB.etapas:
        prompt = e.papel_agente.prompt_base
        assert 10 <= len(prompt.splitlines()) <= 40, e.codigo
        assert "concluir_etapa" in prompt, e.codigo
        for saida in e.saidas:
            assert saida in prompt, (e.codigo, saida)


# --- aplicavel ----------------------------------------------------------------------------


def test_aplicavel_semente():
    av = PB.aplicavel(IDEIA_SEMENTE)
    assert av.aplicavel and av.pontuacao == 1.0 and av.lacunas == []


def test_nao_aplicavel_sem_fonte():
    av = PB.aplicavel(replace(IDEIA_SEMENTE, fontes_candidatas=[]))
    assert not av.aplicavel and any("fonte" in lacuna for lacuna in av.lacunas)
    assert av.pontuacao == pytest.approx(2 / 3)


def test_nao_aplicavel_sem_pergunta_norteadora():
    av = PB.aplicavel(replace(IDEIA_SEMENTE, pergunta_norteadora="  "))
    assert not av.aplicavel and any("pergunta" in lacuna for lacuna in av.lacunas)


def test_tipo_incompativel_ainda_aplicavel_mas_pontua_menos():
    av = PB.aplicavel(replace(IDEIA_SEMENTE, tipo="oportunidade"))
    assert av.aplicavel
    assert av.pontuacao == 0.5
    assert any("tipo" in lacuna for lacuna in av.lacunas)


def test_pontuacao_fica_entre_zero_e_um():
    av = PB.aplicavel(
        replace(IDEIA_SEMENTE, tipo="oportunidade", pergunta_norteadora="", fontes_candidatas=[])
    )
    assert not av.aplicavel and 0 <= av.pontuacao <= 1


# --- instanciar ---------------------------------------------------------------------------


def test_instanciar_cronograma():
    e = PB.instanciar(IDEIA_SEMENTE)
    assert [m.codigo for m in e.cronograma] == ["M1", "M2", "M3", "M4", "M5", "M6"]
    assert {m.etapa_codigo for m in e.cronograma if m.exige_gate} == {
        "coleta_dados",
        "modelagem",
        "relatorio",
        "retroalimentacao",
    }
    assert [m.etapa_codigo for m in e.cronograma] == [
        "coleta_dados",
        "analise_exploratoria",
        "modelagem",
        "sintese",
        "relatorio",
        "retroalimentacao",
    ]


def test_instanciar_marcos_herdam_da_etapa_e_definem_responsavel():
    marcos = {m.codigo: m for m in PB.instanciar(IDEIA_SEMENTE).cronograma}
    m1, m2 = marcos["M1"], marcos["M2"]
    assert m1.responsavel == "humano:responsavel_por_dados"
    assert m1.papel_revisor == "responsavel_por_dados"
    assert m1.criterio_saida == etapa("coleta_dados").criterio_saida
    assert m1.nome == etapa("coleta_dados").nome
    assert m2.responsavel == "agente:analista" and m2.papel_revisor is None
    assert marcos["M6"].responsavel == "humano:curador_do_acervo"
    assert all(m.data_planejada is None for m in marcos.values())


def test_instanciar_copia_problema_metodologia_e_fontes():
    e = PB.instanciar(IDEIA_SEMENTE)
    assert e.definicao_problema.pergunta_norteadora == IDEIA_SEMENTE.pergunta_norteadora
    assert e.definicao_problema.contexto == IDEIA_SEMENTE.descricao
    assert (e.metodologia.playbook_codigo, e.metodologia.playbook_versao) == (
        "projeto-academico-simples",
        "0.1.0",
    )
    assert [f.nome for f in e.fontes_dados] == [f["nome"] for f in IDEIA_SEMENTE.fontes_candidatas]
    assert len(e.fontes_dados) == 10
    assert all(f.status_acesso == "nao_testado" and f.url is None for f in e.fontes_dados)
    assert all(f.licenca == f.cobertura == f.periodicidade == "" for f in e.fontes_dados)


def test_instanciar_preserva_url_da_fonte_e_deixa_o_resto_vazio():
    ideia = replace(IDEIA_SEMENTE, fontes_candidatas=[{"nome": "SIDRA", "url": "https://x.gov/s"}])
    e = PB.instanciar(ideia)
    assert e.fontes_dados[0].url == "https://x.gov/s"
    assert e.objetivos == e.restricoes == e.riscos == e.resultados_esperados == []
    assert e.escopo.dentro == [] and e.definicao_problema.hipoteses == []
    assert EspecProjeto.model_validate(e.model_dump(mode="json")) == e


def test_instanciar_e_deterministico():
    assert PB.instanciar(IDEIA_SEMENTE) == PB.instanciar(IDEIA_SEMENTE)


# --- validar_etapa ------------------------------------------------------------------------


def test_validar_etapa_falta_saida():
    r = PB.validar_etapa(etapa("harmonizacao"), SaidaEtapa({"dataset_analitico": REF}, ""))
    assert not r.aprovado
    texto = " ".join(r.feedback)
    assert "dicionario_dados" in texto and "log_tratamento" in texto


def test_validar_etapa_sem_checagem_especifica_aprova_com_todas_as_saidas():
    r = PB.validar_etapa(etapa("harmonizacao"), saida_com("harmonizacao"))
    assert r.aprovado and r.feedback == []


def test_validar_coleta_aprova_catalogo_completo(tmp_path):
    catalogo = ref_json(
        tmp_path,
        "catalogo_fontes",
        [{"nome": "IBGE", "licenca": "CC-BY", "cobertura": "Brasil, 2022"}],
    )
    assert PB.validar_etapa(
        etapa("coleta_dados"), saida_com("coleta_dados", catalogo_fontes=catalogo)
    ).aprovado


def test_validar_coleta_reprova_item_sem_licenca_ou_cobertura(tmp_path):
    catalogo = ref_json(
        tmp_path,
        "catalogo_fontes",
        [
            {"nome": "IBGE", "licenca": "CC-BY", "cobertura": "Brasil"},
            {"nome": "Conab", "cobertura": "Brasil"},
            {"nome": "MAPA", "licenca": "  ", "cobertura": ""},
        ],
    )
    r = PB.validar_etapa(etapa("coleta_dados"), saida_com("coleta_dados", catalogo_fontes=catalogo))
    texto = "\n".join(r.feedback)
    assert not r.aprovado
    assert "Conab" in texto and "licenca" in texto
    assert "MAPA" in texto and "cobertura" in texto
    assert "IBGE" not in texto


def test_validar_coleta_reprova_catalogo_vazio(tmp_path):
    catalogo = ref_json(tmp_path, "catalogo_fontes", [])
    r = PB.validar_etapa(etapa("coleta_dados"), saida_com("coleta_dados", catalogo_fontes=catalogo))
    assert not r.aprovado and "vazio" in " ".join(r.feedback)


@pytest.mark.parametrize(
    "artefato",
    [
        REF,  # arquivo inexistente
        ArtefatoRef("s3://bucket/x", "0" * 64, "x", "json"),  # esquema ainda não suportado
    ],
)
def test_validar_coleta_nao_levanta_com_artefato_ilegivel(artefato):
    r = PB.validar_etapa(etapa("coleta_dados"), saida_com("coleta_dados", catalogo_fontes=artefato))
    assert not r.aprovado and "catalogo_fontes" in " ".join(r.feedback)


@pytest.mark.parametrize(
    "conteudo", ["{não é json", json.dumps({"nome": "IBGE"}), json.dumps(["texto"])]
)
def test_validar_coleta_reprova_json_invalido_ou_de_forma_errada(tmp_path, conteudo):
    ref = ref_json(tmp_path, "catalogo_fontes", conteudo)
    r = PB.validar_etapa(etapa("coleta_dados"), saida_com("coleta_dados", catalogo_fontes=ref))
    assert not r.aprovado and "catalogo_fontes" in " ".join(r.feedback)


def test_validar_coleta_acumula_saida_faltante_e_catalogo_ruim(tmp_path):
    catalogo = ref_json(tmp_path, "catalogo_fontes", [{"nome": "X"}])
    r = PB.validar_etapa(etapa("coleta_dados"), SaidaEtapa({"catalogo_fontes": catalogo}, ""))
    texto = " ".join(r.feedback)
    assert not r.aprovado and "dados_brutos" in texto and "licenca" in texto


CENARIOS_OK = {
    "baixo": {"intervalo": [1, 2], "premissas": ["a"]},
    "medio": {"intervalo": [2, 3]},
    "alto": {"intervalo": [3, 4]},
}


def test_validar_modelagem_aprova_tres_cenarios(tmp_path):
    ref = ref_json(tmp_path, "cenarios", CENARIOS_OK)
    assert PB.validar_etapa(etapa("modelagem"), saida_com("modelagem", cenarios=ref)).aprovado


def test_validar_modelagem_sem_cenario_alto(tmp_path):
    ref = ref_json(tmp_path, "cenarios", {k: v for k, v in CENARIOS_OK.items() if k != "alto"})
    r = PB.validar_etapa(etapa("modelagem"), saida_com("modelagem", cenarios=ref))
    assert not r.aprovado and "alto" in " ".join(r.feedback)


def test_validar_modelagem_cenario_sem_intervalo(tmp_path):
    cenarios = {**CENARIOS_OK, "medio": {"premissas": ["a"]}}
    ref = ref_json(tmp_path, "cenarios", cenarios)
    r = PB.validar_etapa(etapa("modelagem"), saida_com("modelagem", cenarios=ref))
    texto = " ".join(r.feedback)
    assert not r.aprovado and "medio" in texto and "intervalo" in texto
    assert "baixo" not in texto


def test_validar_modelagem_com_cenarios_ilegiveis_nao_levanta():
    r = PB.validar_etapa(etapa("modelagem"), saida_com("modelagem"))
    assert not r.aprovado and "cenarios" in " ".join(r.feedback)


# --- validar_etapa: retroalimentacao ------------------------------------------------------

IDEIA_OK = {
    "titulo": "Estender a análise até 2060",
    "tipo": "pergunta_de_pesquisa",
    "descricao": "Repetir o estudo com horizonte maior.",
    "pergunta_norteadora": "Como a oferta de água evolui até 2060 nas mesmas bacias estudadas?",
    "dominio": ["agro"],
    "valor": 4,
    "viabilidade": 3,
}
NOVAS_OK = [
    {"proximo_passo": "Estender a 2060", "acao": "ideia", "ideia": IDEIA_OK},
    {
        "proximo_passo": "Refazer com microdados",
        "acao": "descartar",
        "motivo_descarte": "sem dados",
    },
]
LICOES_OK = [{"tipo": "funcionou", "descricao": "Harmonizar cedo", "evidencia": "etapa 2"}]


def _validar_retro(tmp_path, novas, licoes):
    refs = {
        "novas_ideias": ref_json(tmp_path, "novas_ideias", novas),
        "licoes_aprendidas": ref_json(tmp_path, "licoes_aprendidas", licoes),
    }
    return PB.validar_etapa(etapa("retroalimentacao"), saida_com("retroalimentacao", **refs))


def test_validar_retroalimentacao_aprova_formato_correto(tmp_path):
    assert _validar_retro(tmp_path, NOVAS_OK, LICOES_OK).aprovado


@pytest.mark.parametrize(
    ("novas", "trecho"),
    [
        ([], "vazio"),
        ({"a": 1}, "lista JSON"),
        ([{"proximo_passo": "x", "acao": "talvez"}], "'acao'"),
        ([{"proximo_passo": "x", "acao": "descartar"}], "motivo_descarte"),
        ([{"proximo_passo": "x", "acao": "ideia"}], "'ideia'"),
        ([{"proximo_passo": "x", "acao": "ideia", "ideia": {**IDEIA_OK, "valor": 9}}], "'valor'"),
        ([{"proximo_passo": "x", "acao": "ideia", "ideia": {**IDEIA_OK, "tipo": "x"}}], "'tipo'"),
        (
            [{"proximo_passo": "x", "acao": "ideia", "ideia": {**IDEIA_OK, "dominio": []}}],
            "dominio",
        ),
        (
            [{"proximo_passo": "x", "acao": "ideia", "ideia": {**IDEIA_OK, "titulo": "t" * 101}}],
            "100",
        ),
        ([{"acao": "descartar", "motivo_descarte": "m"}], "proximo_passo"),
    ],
)
def test_validar_retroalimentacao_reprova_novas_ideias_mal_formadas(tmp_path, novas, trecho):
    r = _validar_retro(tmp_path, novas, LICOES_OK)
    assert not r.aprovado
    assert trecho in " ".join(r.feedback)


@pytest.mark.parametrize(
    ("licoes", "trecho"),
    [
        ([], "vazio"),
        ([{"tipo": "outro", "descricao": "d", "evidencia": "e"}], "'tipo'"),
        ([{"tipo": "falhou", "descricao": "d"}], "evidencia"),
        ([{"tipo": "falhou", "evidencia": "e"}], "descricao"),
    ],
)
def test_validar_retroalimentacao_reprova_licoes_mal_formadas(tmp_path, licoes, trecho):
    r = _validar_retro(tmp_path, NOVAS_OK, licoes)
    assert not r.aprovado
    assert trecho in " ".join(r.feedback)


def test_validar_retroalimentacao_acumula_os_dois_artefatos_e_nao_levanta_com_ilegivel(tmp_path):
    r = _validar_retro(tmp_path, [], "{nao e json")
    texto = " ".join(r.feedback)
    assert not r.aprovado and "novas_ideias" in texto and "licoes_aprendidas" in texto
    assert not PB.validar_etapa(etapa("retroalimentacao"), saida_com("retroalimentacao")).aprovado


def test_prompt_da_retroalimentacao_documenta_o_formato_json():
    prompt = etapa("retroalimentacao").papel_agente.prompt_base
    for campo in ("proximo_passo", "acao", "motivo_descarte", "pergunta_norteadora", "evidencia"):
        assert campo in prompt, campo
