from ais4.criacao.validacao import ResultadoCritica
from ais4.llm.falso import ClienteLLMFalso
from tests.criacao.conftest import cabecalho
from tests.criacao.fabrica import (
    URL_CENSO,
    consultar,
    espec_completa,
    qualificar_semente_com_uma_fonte,
    verificacao_acessivel,
)
from tests.playbooks.semente import IDEIA_SEMENTE
from tests.projetos.fabrica import RESPONSAVEIS

ESPEC = espec_completa(IDEIA_SEMENTE)
RESP_GATES = {k: v for k, v in RESPONSAVEIS.items() if k != "dono_do_projeto"}


def _roteiro():
    return ClienteLLMFalso(
        {
            "verificador_fontes": [verificacao_acessivel()],
            "redator_projeto": [ESPEC],
            "critico_projeto": [ResultadoCritica(lacunas=[])],
        }
    )


def _ciclo(api, llm_da_rota, quem="tati"):
    qualificar_semente_com_uma_fonte()
    llm_da_rota.cliente = _roteiro()
    r = api.post("/criacao/ciclos", headers=cabecalho(quem))
    assert r.status_code == 200, r.text
    return r.json()["proposta_id"]


def test_ciclo_exige_papel_responsavel_tecnico(api):
    assert api.post("/criacao/ciclos", headers=cabecalho("ana")).status_code == 403
    assert api.post("/criacao/ciclos").status_code == 401


def test_ciclo_sem_ideia_devolve_proposta_nula(api):
    r = api.post("/criacao/ciclos", headers=cabecalho("tati"))
    assert (r.status_code, r.json()) == (200, {"proposta_id": None})


def test_ciclo_usa_o_chamador_como_solicitante(api, llm_da_rota, banco):
    proposta_id = _ciclo(api, llm_da_rota)
    assert consultar(banco, "select solicitado_por from criacao.proposta") == [("tati",)]
    assert proposta_id


def test_listar_e_obter_proposta(api, llm_da_rota):
    proposta_id = _ciclo(api, llm_da_rota)

    lista = api.get("/criacao/propostas?status=aguardando_aprovacao", headers=cabecalho("ana"))
    assert [p["id"] for p in lista.json()] == [proposta_id]
    vazia = api.get("/criacao/propostas?status=aprovada", headers=cabecalho("ana"))
    assert vazia.json() == []

    r = api.get(f"/criacao/propostas/{proposta_id}", headers=cabecalho("ana"))
    corpo = r.json()
    assert corpo["proposta"]["status"] == "aguardando_aprovacao"
    assert corpo["proposta"]["conteudo"]["objetivos"]
    assert [i["n"] for i in corpo["iteracoes"]] == [1]
    assert corpo["fontes"][0]["url"] == URL_CENSO and corpo["fontes"][0]["status"] == "acessivel"


def test_status_invalido_e_proposta_inexistente(api):
    assert api.get("/criacao/propostas?status=xyz", headers=cabecalho("ana")).status_code == 422
    inexistente = "00000000-0000-4000-8000-0000000000ff"
    r = api.get(f"/criacao/propostas/{inexistente}", headers=cabecalho("ana"))
    assert r.status_code == 404


def test_decisao_aprovar_cria_projeto(api, llm_da_rota):
    proposta_id = _ciclo(api, llm_da_rota)

    r = api.post(
        f"/criacao/propostas/{proposta_id}/decisao",
        json={"acao": "aprovar", "responsaveis": RESP_GATES},
        headers=cabecalho("ana"),
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "aprovada" and r.json()["projeto_id"]


def test_decisao_do_solicitante_e_403_e_sem_papel_dono_tambem(api, llm_da_rota):
    proposta_id = _ciclo(api, llm_da_rota, quem="rafa")  # rafa é dono, mas pediu o ciclo
    corpo = {"acao": "rejeitar", "motivo": "m"}

    assert (
        api.post(
            f"/criacao/propostas/{proposta_id}/decisao", json=corpo, headers=cabecalho("rafa")
        ).status_code
        == 403
    )
    assert (
        api.post(
            f"/criacao/propostas/{proposta_id}/decisao", json=corpo, headers=cabecalho("rita")
        ).status_code
        == 403
    )


def test_decisao_rejeitar_sem_motivo_e_409(api, llm_da_rota):
    proposta_id = _ciclo(api, llm_da_rota)

    r = api.post(
        f"/criacao/propostas/{proposta_id}/decisao",
        json={"acao": "rejeitar"},
        headers=cabecalho("ana"),
    )

    assert r.status_code == 409
    assert "motivo" in r.json()["erro"]


def test_decisao_com_acao_invalida_e_422(api, llm_da_rota):
    proposta_id = _ciclo(api, llm_da_rota)
    r = api.post(
        f"/criacao/propostas/{proposta_id}/decisao",
        json={"acao": "talvez"},
        headers=cabecalho("ana"),
    )
    assert r.status_code == 422
