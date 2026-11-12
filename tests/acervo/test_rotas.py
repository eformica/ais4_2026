import hashlib

from tests.playbooks.semente import IDEIA_SEMENTE

SEMENTE = str(IDEIA_SEMENTE.id)
INEXISTENTE = "00000000-0000-4000-8000-00000000dead"
CORPO = {
    "titulo": "Ideia via API",
    "tipo": "hipotese",
    "descricao": "Contexto.",
    "pergunta_norteadora": "Por quê?",
    "dominio": ["agro"],
    "valor": 4,
    "viabilidade": 5,
}


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _criar(cliente, token="t-ana", **kw) -> dict:
    r = cliente.post("/acervo/ideias", json={**CORPO, **kw}, headers=_h(token))
    assert r.status_code == 201, r.text
    return r.json()


def _transicao(cliente, id_, para, token, **kw):
    return cliente.post(
        f"/acervo/ideias/{id_}/transicao", json={"para": para, **kw}, headers=_h(token)
    )


def test_exige_autenticacao(cliente):
    assert cliente.get("/acervo/ideias").status_code == 401
    assert cliente.post("/acervo/ideias", json=CORPO).status_code == 401
    assert cliente.get(f"/acervo/ideias/{SEMENTE}").status_code == 401


def test_criar_e_obter(cliente):
    criada = _criar(cliente)
    assert criada["status"] == "rascunho"
    assert criada["criado_por"] == "ana"
    assert float(criada["prioridade"]) == 20
    r = cliente.get(f"/acervo/ideias/{criada['id']}", headers=_h("t-bruno"))
    assert r.status_code == 200
    assert r.json() == criada


def test_titulo_101_caracteres_422(cliente):
    r = cliente.post("/acervo/ideias", json={**CORPO, "titulo": "x" * 101}, headers=_h("t-ana"))
    assert r.status_code == 422


def test_criar_com_campos_invalidos_422(cliente):
    for extra in ({"valor": 6}, {"tipo": "xyz"}, {"dominio": []}, {"status": "qualificada"}):
        r = cliente.post("/acervo/ideias", json={**CORPO, **extra}, headers=_h("t-ana"))
        assert r.status_code == 422, extra


def test_obter_inexistente_404(cliente):
    r = cliente.get(f"/acervo/ideias/{INEXISTENTE}", headers=_h("t-ana"))
    assert r.status_code == 404
    assert "erro" in r.json()


def test_listar_fila_por_status_e_limite(cliente):
    r = cliente.get("/acervo/ideias?status=triagem", headers=_h("t-ana"))
    assert [i["id"] for i in r.json()] == [SEMENTE]
    assert cliente.get("/acervo/ideias", headers=_h("t-ana")).json() == []  # padrão: qualificada
    assert cliente.get("/acervo/ideias?limite=0", headers=_h("t-ana")).status_code == 422
    assert cliente.get("/acervo/ideias?status=xyz", headers=_h("t-ana")).status_code == 422
    for i in range(3):
        _criar(cliente, titulo=f"t{i}", status="triagem")
    r = cliente.get("/acervo/ideias?status=triagem&limite=2", headers=_h("t-ana"))
    assert len(r.json()) == 2


def test_patch_altera_so_o_enviado(cliente):
    criada = _criar(cliente)
    r = cliente.patch(
        f"/acervo/ideias/{criada['id']}",
        json={"titulo": "Novo", "prioridade_manual": 30},
        headers=_h("t-bruno"),
    )
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["titulo"] == "Novo"
    assert float(corpo["prioridade"]) == 30
    assert corpo["descricao"] == criada["descricao"]
    assert corpo["status"] == "rascunho"


def test_patch_nao_muda_status_e_rejeita_null_e_faixa(cliente):
    criada = _criar(cliente)
    url = f"/acervo/ideias/{criada['id']}"
    r = cliente.patch(url, json={"status": "arquivada"}, headers=_h("t-ana"))
    assert r.status_code == 200  # campo fora de IdeiaAtualizar é ignorado
    assert r.json()["status"] == "rascunho"
    assert cliente.patch(url, json={"titulo": None}, headers=_h("t-ana")).status_code == 422
    assert cliente.patch(url, json={"valor": 9}, headers=_h("t-ana")).status_code == 422


def test_patch_inexistente_404(cliente):
    r = cliente.patch(f"/acervo/ideias/{INEXISTENTE}", json={"titulo": "x"}, headers=_h("t-ana"))
    assert r.status_code == 404


def test_transicao_para_qualificada_pela_rota_generica_409(cliente):
    # a semente está em triagem e tem fontes: no banco a transição seria válida
    r = _transicao(cliente, SEMENTE, "qualificada", "t-carla")
    assert r.status_code == 409
    assert "qualifica" in r.json()["erro"].lower()
    status = cliente.get(f"/acervo/ideias/{SEMENTE}", headers=_h("t-carla")).json()["status"]
    assert status == "triagem"
    # o 409 vem antes de qualquer consulta: vale até para id inexistente
    assert _transicao(cliente, INEXISTENTE, "qualificada", "t-carla").status_code == 409


def test_transicao_para_status_desconhecido_422(cliente):
    assert _transicao(cliente, SEMENTE, "xyz", "t-carla").status_code == 422


def test_arquivar_exige_curador_e_motivo(cliente):
    id_ = _criar(cliente)["id"]
    assert _transicao(cliente, id_, "arquivada", "t-ana", motivo="x").status_code == 403
    assert _transicao(cliente, id_, "arquivada", "t-carla").status_code == 409
    r = _transicao(cliente, id_, "arquivada", "t-carla", motivo="Fora de escopo")
    assert r.status_code == 200
    assert r.json()["status"] == "arquivada"
    assert r.json()["motivo_status"] == "Fora de escopo"


def test_fundir_pela_rota_registra_destino(cliente):
    a, b = _criar(cliente, titulo="a"), _criar(cliente, titulo="b")
    r = _transicao(cliente, a["id"], "fundida", "t-ana", fundida_em_id=b["id"])
    assert r.status_code == 403
    r = _transicao(cliente, a["id"], "fundida", "t-carla", fundida_em_id=b["id"], motivo="Igual")
    assert r.status_code == 200
    assert r.json()["fundida_em_id"] == b["id"]


def test_rascunho_para_triagem_autor_ou_curador(cliente):
    da_ana = _criar(cliente, token="t-ana")
    # outro usuário sem papel não pode
    assert _transicao(cliente, da_ana["id"], "triagem", "t-bruno").status_code == 403
    r = _transicao(cliente, da_ana["id"], "triagem", "t-ana")
    assert r.status_code == 200
    assert r.json()["status"] == "triagem"
    # curador pode enviar ideia de outro autor
    outra = _criar(cliente, token="t-ana", titulo="outra")
    assert _transicao(cliente, outra["id"], "triagem", "t-carla").status_code == 200


def test_triagem_para_rascunho_e_outras_transicoes_so_curador(cliente):
    id_ = _criar(cliente, status="triagem")["id"]
    # devolver ao autor: nem o próprio autor
    assert _transicao(cliente, id_, "rascunho", "t-ana", motivo="m").status_code == 403
    r = _transicao(cliente, id_, "rascunho", "t-carla", motivo="Faltam dados")
    assert r.status_code == 200
    assert r.json()["status"] == "rascunho"
    # em_projeto: só curador; e o trigger recusa a transição inválida
    assert _transicao(cliente, id_, "em_projeto", "t-ana").status_code == 403
    assert _transicao(cliente, id_, "em_projeto", "t-carla").status_code == 409


def test_reabrir_arquivada_exige_curador(cliente):
    id_ = _criar(cliente)["id"]
    _transicao(cliente, id_, "arquivada", "t-carla", motivo="m")
    assert _transicao(cliente, id_, "triagem", "t-ana").status_code == 403
    assert _transicao(cliente, id_, "triagem", "t-carla").status_code == 200


def test_autor_nao_envia_a_triagem_ideia_de_outro_a_partir_de_triagem(cliente):
    # triagem -> triagem não é "rascunho -> triagem": exige curador
    id_ = _criar(cliente, status="triagem")["id"]
    assert _transicao(cliente, id_, "triagem", "t-ana").status_code == 403


def test_transicao_inexistente_404(cliente):
    assert _transicao(cliente, INEXISTENTE, "triagem", "t-carla").status_code == 404


def test_historico_pela_rota(cliente):
    criada = _criar(cliente)
    _transicao(cliente, criada["id"], "triagem", "t-ana")
    r = cliente.get(f"/acervo/ideias/{criada['id']}/historico", headers=_h("t-bruno"))
    assert r.status_code == 200
    assert [(e["evento"], e["para_status"], e["autor"]) for e in r.json()] == [
        ("criada", "rascunho", "ana"),
        ("status_alterado", "triagem", "ana"),
    ]
    r = cliente.get(f"/acervo/ideias/{INEXISTENTE}/historico", headers=_h("t-ana"))
    assert r.status_code == 404


# --- enriquecimento (1.1, R28) -------------------------------------------------------------
def _usar_llm(cliente, *itens):
    from ais4.acervo.rotas import obter_cliente_llm
    from ais4.llm.falso import ClienteLLMFalso

    llm = ClienteLLMFalso({"enriquecedor": list(itens)})
    cliente.app.dependency_overrides[obter_cliente_llm] = lambda: llm
    return llm


def _sugestao():
    from ais4.processos.alimentacao_acervo import FonteSugerida, SugestaoEnriquecimento

    return SugestaoEnriquecimento(
        dominio=["clima"],
        fontes=[FonteSugerida(nome="ANA", url=None, cobertura="BR")],
        playbook_codigo="projeto-academico-simples",
        justificativa="j",
    )


def test_rota_enriquecimento_so_para_o_curador(cliente):
    llm = _usar_llm(cliente, _sugestao())
    ideia = _criar(cliente)
    url = f"/acervo/ideias/{ideia['id']}/enriquecimento"

    assert cliente.post(url).status_code == 401
    assert cliente.post(url, headers=_h("t-ana")).status_code == 403
    assert llm.chamadas == []


def test_rota_enriquecimento_acrescenta_e_devolve_o_resultado(cliente, embedder_falso):
    llm = _usar_llm(cliente, _sugestao())
    ideia = _criar(cliente)

    r = cliente.post(f"/acervo/ideias/{ideia['id']}/enriquecimento", headers=_h("t-carla"))

    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["dominio_sugerido"] == ["clima"]
    assert corpo["fontes_sugeridas"] == [{"nome": "ANA", "cobertura": "BR"}]
    assert corpo["playbook_sugerido"] == "projeto-academico-simples"
    assert corpo["candidatas_duplicidade"] == []
    atual = cliente.get(f"/acervo/ideias/{ideia['id']}", headers=_h("t-ana")).json()
    assert atual["dominio"] == ["agro", "clima"] and atual["status"] == "rascunho"
    assert len(llm.chamadas) == 1


def test_rota_enriquecimento_ideia_inexistente_404_e_status_errado_409(cliente):
    _usar_llm(cliente, _sugestao())

    assert (
        cliente.post(
            f"/acervo/ideias/{INEXISTENTE}/enriquecimento", headers=_h("t-carla")
        ).status_code
        == 404
    )
    ideia = _criar(cliente)
    assert (
        _transicao(cliente, ideia["id"], "arquivada", "t-carla", motivo="teste").status_code == 200
    )
    r = cliente.post(f"/acervo/ideias/{ideia['id']}/enriquecimento", headers=_h("t-carla"))
    assert r.status_code == 409
