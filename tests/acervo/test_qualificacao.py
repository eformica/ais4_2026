from uuid import UUID, uuid4

import psycopg
import pytest

from ais4.acervo import servico
from ais4.acervo.servico import IdeiaCriar, ResultadoQualificacao
from ais4.acervo.similaridade import EmbedderFalso, texto_para_embedding
from ais4.auth import Usuario
from ais4.comum.dto import IdeiaDTO, LacunaDTO
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.playbooks import registro
from tests.acervo.test_rotas import CORPO, INEXISTENTE, _h
from tests.acervo.vetores import vetor
from tests.playbooks.semente import IDEIA_SEMENTE

SEMENTE = IDEIA_SEMENTE.id
CARLA = Usuario("carla", frozenset({"curador_do_acervo"}))
ANA = Usuario("ana", frozenset({"dono_do_projeto"}))
PERGUNTA = "Como as mudanças demográficas afetam a disponibilidade regional de alimentos no país?"
FONTE = [{"nome": "IBGE - Censo 2022"}]


def _ideia(titulo="Ideia", autor="ana", **kw) -> IdeiaDTO:
    campos = {
        "titulo": titulo,
        "tipo": "pergunta_de_pesquisa",
        "descricao": f"Descrição de {titulo}.",
        "pergunta_norteadora": PERGUNTA,
        "dominio": ["agro"],
        "valor": 3,
        "viabilidade": 3,
        "fontes_candidatas": FONTE,
        "status": "triagem",
    }
    campos.update(kw)
    return servico.criar_ideia(IdeiaCriar(**campos), autor)


def _embedder(*pares: tuple[IdeiaDTO, list[float]]) -> EmbedderFalso:
    return EmbedderFalso({texto_para_embedding(i): v for i, v in pares})


def _tags(r: ResultadoQualificacao) -> list[str]:
    return [p.split(":")[0] for p in r.pendencias]


def _duplicadas() -> tuple[IdeiaDTO, IdeiaDTO, EmbedderFalso]:
    """`nova` (triagem) e `existente` com vetores quase idênticos (cosseno ~0,99)."""
    nova, existente = _ideia("nova"), _ideia("existente", status="rascunho")
    return nova, existente, _embedder((nova, vetor(1.0)), (existente, vetor(1.0, 0.1)))


# --- caminho feliz -------------------------------------------------------------------------
def test_qualificar_semente_sem_duplicatas_ok(banco, limpar):
    r = servico.qualificar(SEMENTE, CARLA, EmbedderFalso())
    assert isinstance(r, ResultadoQualificacao)
    assert r.qualificada
    assert r.pendencias == []
    assert r.candidatas_duplicidade == []
    assert servico.obter(SEMENTE).status == "qualificada"
    ultimo = servico.historico(SEMENTE)[-1]
    assert (ultimo.de_status, ultimo.para_status, ultimo.autor) == (
        "triagem",
        "qualificada",
        "carla",
    )


def test_qualificar_indexa_a_ideia(banco, limpar):
    servico.qualificar(SEMENTE, CARLA, EmbedderFalso())
    with psycopg.connect(banco) as c:
        assert c.execute("select ideia_id from acervo.ideia_embedding").fetchall() == [(SEMENTE,)]


# --- permissões e estado ---------------------------------------------------------------------
def test_autor_nao_qualifica_propria_ideia(banco, limpar):
    propria = _ideia(autor="carla")
    with pytest.raises(Proibido):
        servico.qualificar(propria.id, CARLA, EmbedderFalso())
    assert servico.obter(propria.id).status == "triagem"


def test_qualificar_exige_papel_curador(banco, limpar):
    with pytest.raises(Proibido):
        servico.qualificar(SEMENTE, ANA, EmbedderFalso())


def test_qualificar_exige_status_triagem(banco, limpar):
    rascunho = _ideia(status="rascunho")
    with pytest.raises(RegraViolada, match="triagem"):
        servico.qualificar(rascunho.id, CARLA, EmbedderFalso())


def test_qualificar_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        servico.qualificar(uuid4(), CARLA, EmbedderFalso())


# --- critérios 1 e 2 ----------------------------------------------------------------------
def test_pergunta_sem_interrogacao_ou_curta_gera_pendencia(banco, limpar):
    sem_ponto = _ideia("a", pergunta_norteadora=PERGUNTA[:-1])
    curta = _ideia("b", pergunta_norteadora="Qual o impacto do clima?")
    for ideia in (sem_ponto, curta):
        r = servico.qualificar(ideia.id, CARLA, EmbedderFalso())
        assert not r.qualificada
        assert _tags(r) == ["pergunta"]
        assert servico.obter(ideia.id).status == "triagem"


def test_pergunta_com_oito_palavras_e_interrogacao_passa(banco, limpar):
    ideia = _ideia(pergunta_norteadora="Um dois três quatro cinco seis sete oito?")
    assert servico.qualificar(ideia.id, CARLA, EmbedderFalso()).qualificada


def test_sem_fonte_candidata_gera_pendencia(banco, limpar):
    ideia = _ideia(fontes_candidatas=[])
    r = servico.qualificar(ideia.id, CARLA, EmbedderFalso())
    assert not r.qualificada
    assert "fonte" in _tags(r)


# --- critério 3: duplicidade -----------------------------------------------------------------
def test_qualificar_bloqueia_duplicata_nao_revisada(banco, limpar):
    nova, existente, emb = _duplicadas()
    r = servico.qualificar(nova.id, CARLA, emb)
    assert not r.qualificada
    assert _tags(r) == ["duplicidade"]
    assert [c for c, _ in r.candidatas_duplicidade] == [existente.id]
    assert r.candidatas_duplicidade[0][1] >= 0.85
    assert str(existente.id) in r.pendencias[0]
    assert servico.obter(nova.id).status == "triagem"


def test_qualificar_com_duplicata_revisada_distinta_ok(banco, limpar):
    nova, existente, emb = _duplicadas()
    servico.registrar_revisao_duplicidade(nova.id, existente.id, 0.99, "distinta", "carla")
    r = servico.qualificar(nova.id, CARLA, emb)
    assert r.qualificada
    assert r.pendencias == []
    assert servico.obter(nova.id).status == "qualificada"


def test_duplicata_marcada_duplicada_continua_bloqueando(banco, limpar):
    nova, existente, emb = _duplicadas()
    servico.registrar_revisao_duplicidade(nova.id, existente.id, 0.99, "duplicada", "carla")
    r = servico.qualificar(nova.id, CARLA, emb)
    assert not r.qualificada
    assert _tags(r) == ["duplicidade"]


def test_ideia_abaixo_do_limiar_nao_e_candidata(banco, limpar):
    nova, outra = _ideia("nova"), _ideia("outra")
    emb = _embedder((nova, vetor(1.0)), (outra, vetor(1.0, 1.0)))  # cosseno ~0,71
    r = servico.qualificar(nova.id, CARLA, emb)
    assert r.qualificada
    assert r.candidatas_duplicidade == []


def test_revisao_posterior_substitui_a_anterior(banco, limpar):
    nova, existente, _ = _duplicadas()
    servico.registrar_revisao_duplicidade(nova.id, existente.id, 0.9, "duplicada", "carla")
    servico.registrar_revisao_duplicidade(nova.id, existente.id, 0.95, "distinta", "diego")
    with psycopg.connect(banco) as c:
        linhas = c.execute(
            "select decisao, revisor, similaridade from acervo.ideia_revisao_duplicidade"
        ).fetchall()
    assert linhas == [("distinta", "diego", pytest.approx(0.95))]


def test_revisao_de_duplicidade_valida_ideias_e_decisao(banco, limpar):
    a = _ideia("a")
    with pytest.raises(NaoEncontrado):
        servico.registrar_revisao_duplicidade(a.id, uuid4(), 0.9, "distinta", "carla")
    with pytest.raises(NaoEncontrado):
        servico.registrar_revisao_duplicidade(uuid4(), a.id, 0.9, "distinta", "carla")
    with pytest.raises(RegraViolada):
        servico.registrar_revisao_duplicidade(a.id, a.id, 0.9, "distinta", "carla")
    b = _ideia("b")
    with pytest.raises(RegraViolada):
        servico.registrar_revisao_duplicidade(a.id, b.id, 0.9, "talvez", "carla")


# --- critério 4: playbook ou lacuna -------------------------------------------------------------
def test_sem_playbook_e_sem_lacuna_pendente(banco, limpar):
    ideia = _ideia(tipo="oportunidade", fontes_candidatas=[])  # nenhum playbook a aceita
    r = servico.qualificar(ideia.id, CARLA, EmbedderFalso())
    assert not r.qualificada
    assert "playbook" in _tags(r)


def test_playbook_nao_registrado_na_memoria_e_ignorado(banco, limpar):
    with registro.isolado(vazio=True):
        r = servico.qualificar(SEMENTE, CARLA, EmbedderFalso())
    assert not r.qualificada
    assert _tags(r) == ["playbook"]


def test_lacuna_aberta_satisfaz_criterio_do_playbook(banco, limpar):
    with registro.isolado(vazio=True):
        servico.registrar_lacuna(SEMENTE, "Falta playbook de séries temporais", ["series"], CARLA)
        r = servico.qualificar(SEMENTE, CARLA, EmbedderFalso())
    assert r.qualificada


def test_lacuna_resolvida_nao_satisfaz_criterio(banco, limpar):
    with registro.isolado(vazio=True):
        id_ = servico.registrar_lacuna(SEMENTE, "Falta playbook", ["series"], CARLA)
        with psycopg.connect(banco, autocommit=True) as c:
            c.execute(
                "update acervo.ideia_lacuna set status = 'resolvida', resolvida_em = now() "
                "where id = %s",
                [id_],
            )
        r = servico.qualificar(SEMENTE, CARLA, EmbedderFalso())
    assert not r.qualificada
    assert _tags(r) == ["playbook"]


def test_pendencias_acumulam(banco, limpar):
    ideia = _ideia(pergunta_norteadora="Tema?", fontes_candidatas=[], tipo="oportunidade")
    r = servico.qualificar(ideia.id, CARLA, EmbedderFalso())
    assert set(_tags(r)) == {"pergunta", "fonte", "playbook"}


# --- lacunas ----------------------------------------------------------------------------------
def test_registrar_lacuna_e_listar(banco, limpar):
    id_ = servico.registrar_lacuna(SEMENTE, "Falta playbook geoespacial", ["geo", "raster"], CARLA)
    lista = servico.lacunas()
    assert [x.id for x in lista] == [id_]
    lacuna = lista[0]
    assert isinstance(lacuna, LacunaDTO)
    assert (lacuna.ideia_id, lacuna.status) == (SEMENTE, "aberta")
    assert lacuna.classes_requeridas == ["geo", "raster"]
    assert lacuna.resolvida_em is None
    assert servico.lacunas(status="resolvida") == []
    assert [x.id for x in servico.lacunas(status="aberta")] == [id_]


def test_registrar_lacuna_exige_curador_e_ideia_existente(banco, limpar):
    with pytest.raises(Proibido):
        servico.registrar_lacuna(SEMENTE, "x", [], ANA)
    with pytest.raises(NaoEncontrado):
        servico.registrar_lacuna(uuid4(), "x", [], CARLA)


# --- rotas ------------------------------------------------------------------------------------
def _criar(cliente, token="t-ana", **kw) -> dict:
    corpo = {
        **CORPO,
        "pergunta_norteadora": PERGUNTA,
        "fontes_candidatas": FONTE,
        "status": "triagem",
        **kw,
    }
    r = cliente.post("/acervo/ideias", json=corpo, headers=_h(token))
    assert r.status_code == 201, r.text
    return r.json()


def test_rota_qualificacao_ok_e_status_muda(cliente):
    r = cliente.post(f"/acervo/ideias/{SEMENTE}/qualificacao", headers=_h("t-carla"))
    assert r.status_code == 200, r.text
    assert r.json() == {"qualificada": True, "pendencias": [], "candidatas_duplicidade": []}
    status = cliente.get(f"/acervo/ideias/{SEMENTE}", headers=_h("t-carla")).json()["status"]
    assert status == "qualificada"


def test_rota_qualificacao_devolve_pendencias_sem_mudar_status(cliente):
    id_ = _criar(cliente, pergunta_norteadora="Tema?")["id"]
    r = cliente.post(f"/acervo/ideias/{id_}/qualificacao", headers=_h("t-carla"))
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["qualificada"] is False
    assert corpo["pendencias"][0].startswith("pergunta:")
    assert cliente.get(f"/acervo/ideias/{id_}", headers=_h("t-carla")).json()["status"] == "triagem"


def test_rota_qualificacao_permissoes_e_erros(cliente):
    url = f"/acervo/ideias/{SEMENTE}/qualificacao"
    assert cliente.post(url).status_code == 401
    assert cliente.post(url, headers=_h("t-ana")).status_code == 403
    assert (
        cliente.post(
            f"/acervo/ideias/{INEXISTENTE}/qualificacao", headers=_h("t-carla")
        ).status_code
        == 404
    )
    rascunho = _criar(cliente, status="rascunho")["id"]
    assert (
        cliente.post(f"/acervo/ideias/{rascunho}/qualificacao", headers=_h("t-carla")).status_code
        == 409
    )


def test_rota_similares(cliente, embedder_falso):
    a, b = _criar(cliente, titulo="a"), _criar(cliente, titulo="b")
    embedder_falso.vetores[_texto(a)] = vetor(1.0)
    embedder_falso.vetores[_texto(b)] = vetor(1.0, 0.1)
    r = cliente.get(f"/acervo/ideias/{a['id']}/similares?limite=1", headers=_h("t-bruno"))
    assert r.status_code == 200, r.text
    [item] = r.json()
    assert item["ideia"]["id"] == b["id"]
    assert item["similaridade"] == pytest.approx(0.995, abs=1e-3)
    assert (
        cliente.get(f"/acervo/ideias/{a['id']}/similares?limite=0", headers=_h("t-ana")).status_code
        == 422
    )
    assert (
        cliente.get(f"/acervo/ideias/{INEXISTENTE}/similares", headers=_h("t-ana")).status_code
        == 404
    )
    assert cliente.get(f"/acervo/ideias/{a['id']}/similares").status_code == 401


def _texto(item: dict) -> str:
    return item["titulo"] + "\n" + item["pergunta_norteadora"] + "\n" + item["descricao"]


def test_rota_duplicidade_registra_e_destrava_qualificacao(cliente, embedder_falso):
    nova = _criar(cliente, titulo="nova")
    existente = _criar(cliente, titulo="existente", status="rascunho")
    embedder_falso.vetores[_texto(nova)] = vetor(1.0)
    embedder_falso.vetores[_texto(existente)] = vetor(1.0, 0.1)
    url = f"/acervo/ideias/{nova['id']}"
    bloqueada = cliente.post(f"{url}/qualificacao", headers=_h("t-carla")).json()
    assert not bloqueada["qualificada"]
    assert bloqueada["candidatas_duplicidade"][0][0] == existente["id"]
    r = cliente.post(
        f"{url}/duplicidade",
        json={"candidata_id": existente["id"], "decisao": "distinta"},
        headers=_h("t-carla"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["decisao"] == "distinta"
    assert r.json()["similaridade"] >= 0.85
    assert cliente.post(f"{url}/qualificacao", headers=_h("t-carla")).json()["qualificada"]


def test_rota_duplicidade_permissoes_e_validacao(cliente):
    nova = _criar(cliente, titulo="nova", token="t-carla")  # autora: carla
    outra = _criar(cliente, titulo="outra")
    url = f"/acervo/ideias/{nova['id']}/duplicidade"
    corpo = {"candidata_id": outra["id"], "decisao": "distinta"}
    assert cliente.post(url, json=corpo).status_code == 401
    assert cliente.post(url, json=corpo, headers=_h("t-ana")).status_code == 403  # sem papel
    assert cliente.post(url, json=corpo, headers=_h("t-carla")).status_code == 403  # é a autora
    url_outra = f"/acervo/ideias/{outra['id']}/duplicidade"
    corpo_inv = {"candidata_id": nova["id"], "decisao": "talvez"}
    assert cliente.post(url_outra, json=corpo_inv, headers=_h("t-carla")).status_code == 422
    corpo_inex = {"candidata_id": INEXISTENTE, "decisao": "distinta"}
    assert cliente.post(url_outra, json=corpo_inex, headers=_h("t-carla")).status_code == 404
    mesma = {"candidata_id": outra["id"], "decisao": "distinta"}
    assert cliente.post(url_outra, json=mesma, headers=_h("t-carla")).status_code == 409


def test_rota_lacunas(cliente):
    url = f"/acervo/ideias/{SEMENTE}/lacunas"
    corpo = {"descricao": "Falta playbook de séries", "classes": ["series"]}
    assert cliente.post(url, json=corpo).status_code == 401
    assert cliente.post(url, json=corpo, headers=_h("t-ana")).status_code == 403
    r = cliente.post(url, json=corpo, headers=_h("t-carla"))
    assert r.status_code == 201, r.text
    assert [x.id for x in servico.lacunas()] == [UUID(r.json()["id"])]
    r404 = cliente.post(f"/acervo/ideias/{INEXISTENTE}/lacunas", json=corpo, headers=_h("t-carla"))
    assert r404.status_code == 404
    assert cliente.post(url, json={"classes": []}, headers=_h("t-carla")).status_code == 422


def test_qualificar_indexa_tambem_ideias_ainda_nao_indexadas(banco, limpar):
    """Sem isso, uma duplicata que nunca passou pela qualificação ficaria invisível."""
    nova, existente = _ideia("nova"), _ideia("existente", status="rascunho")
    emb = _embedder((nova, vetor(1.0)), (existente, vetor(0.0, 1.0)))
    r = servico.qualificar(nova.id, CARLA, emb)
    assert r.qualificada
    with psycopg.connect(banco) as c:
        ids = {x[0] for x in c.execute("select ideia_id from acervo.ideia_embedding").fetchall()}
    assert ids == {nova.id, existente.id, SEMENTE}
