from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from ais4.comum.dto import ArtefatoRef
from ais4.projetos import servico
from tests.projetos.conftest import cabecalho
from tests.projetos.fabrica import (
    aprovar_todos_os_gates,
    espec,
    ir_para_revisao,
    levar_a_gate,
    novo_projeto,
    preparar_fechamento,
)


def _spec_json() -> dict:
    return espec().model_dump(mode="json")


def test_exige_autenticacao(cliente):
    p = novo_projeto()
    assert cliente.get("/projetos").status_code == 401
    assert cliente.get(f"/projetos/{p.id}").status_code == 401
    assert cliente.post(f"/projetos/{p.id}/conclusao").status_code == 401


def test_leituras_para_qualquer_usuario_autenticado(cliente):
    p = novo_projeto()
    q = novo_projeto(titulo="Outro")
    servico.transicionar(q.id, "em_execucao", "ana")
    h = cabecalho("fred")  # sem papel algum no projeto

    lista = cliente.get("/projetos", headers=h)
    assert lista.status_code == 200 and {i["id"] for i in lista.json()} == {str(p.id), str(q.id)}
    filtrada = cliente.get("/projetos?status=em_execucao", headers=h).json()
    assert [i["id"] for i in filtrada] == [str(q.id)]
    assert cliente.get("/projetos?status=xyz", headers=h).status_code == 422

    obtido = cliente.get(f"/projetos/{p.id}", headers=h).json()
    assert obtido["titulo"] == "Abastecimento 2050" and obtido["status"] == "aprovado"

    marcos = cliente.get(f"/projetos/{p.id}/marcos", headers=h).json()
    assert [m["codigo"] for m in marcos] == ["M1", "M2", "M3", "M4", "M5", "M6"]

    ref = ArtefatoRef(uri="file://x", sha256="b" * 64, nome="n", tipo="outro")
    servico.registrar_artefato(p.id, None, "outro", "nota", ref, {"k": 1})
    arts = cliente.get(f"/projetos/{p.id}/artefatos", headers=h).json()
    assert [(a["nome"], a["hash_sha256"], a["proveniencia"]) for a in arts] == [
        ("nota", "b" * 64, {"k": 1})
    ]


def test_404_para_projeto_inexistente(cliente):
    h = cabecalho("ana")
    for caminho in ("", "/marcos", "/artefatos", "/especificacao"):
        r = cliente.get(f"/projetos/{uuid4()}{caminho}", headers=h)
        assert r.status_code == 404, caminho
        assert "erro" in r.json()


def test_especificacao_vigente_e_por_versao(cliente):
    p = novo_projeto()
    h = cabecalho("ana")
    r = cliente.get(f"/projetos/{p.id}/especificacao", headers=h).json()
    assert r["versao"] == 1 and r["justificativa"] is None
    assert r["especificacao"] == _spec_json()

    nova = _spec_json() | {"restricoes": [{"tipo": "prazo", "descricao": "6 meses", "metrica": ""}]}
    criada = cliente.post(
        f"/projetos/{p.id}/especificacao",
        headers=h,
        json={"especificacao": nova, "justificativa": "Prazo definido"},
    )
    assert criada.status_code == 201 and criada.json() == {"versao": 2}

    vigente = cliente.get(f"/projetos/{p.id}/especificacao", headers=h).json()
    assert vigente["versao"] == 2 and vigente["justificativa"] == "Prazo definido"
    assert vigente["aprovado_por"] == "ana"
    assert vigente["especificacao"]["restricoes"][0]["descricao"] == "6 meses"
    v1 = cliente.get(f"/projetos/{p.id}/especificacao?versao=1", headers=h).json()
    assert v1["especificacao"] == _spec_json()
    assert cliente.get(f"/projetos/{p.id}/especificacao?versao=9", headers=h).status_code == 404


def test_nova_versao_sem_justificativa_409_e_so_o_dono(cliente):
    p = novo_projeto()  # dono: ana
    corpo = {"especificacao": _spec_json(), "justificativa": "x"}
    # bruno tem o papel, mas não é o dono deste projeto; davi nem tem o papel
    assert (
        cliente.post(
            f"/projetos/{p.id}/especificacao", headers=cabecalho("bruno"), json=corpo
        ).status_code
        == 403
    )
    assert (
        cliente.post(
            f"/projetos/{p.id}/especificacao", headers=cabecalho("davi"), json=corpo
        ).status_code
        == 403
    )

    sem = cliente.post(
        f"/projetos/{p.id}/especificacao",
        headers=cabecalho("ana"),
        json={"especificacao": _spec_json()},
    )
    assert sem.status_code == 409 and "justificativa" in sem.json()["erro"]
    invalida = cliente.post(
        f"/projetos/{p.id}/especificacao",
        headers=cabecalho("ana"),
        json={"especificacao": {"riscos": [{"categoria": "dados"}]}, "justificativa": "x"},
    )
    assert invalida.status_code == 422
    assert [v for v in (servico.obter_especificacao(p.id).versao,)] == [1]


def test_transicao_so_o_dono_e_o_banco_valida(cliente):
    p = novo_projeto()
    url = f"/projetos/{p.id}/transicao"
    assert (
        cliente.post(url, headers=cabecalho("bruno"), json={"para": "em_execucao"}).status_code
        == 403
    )
    ok = cliente.post(url, headers=cabecalho("ana"), json={"para": "em_execucao"})
    assert ok.status_code == 200 and ok.json()["status"] == "em_execucao"
    invalida = cliente.post(url, headers=cabecalho("ana"), json={"para": "concluido"})
    assert invalida.status_code == 409 and "inválida" in invalida.json()["erro"]
    pausa = cliente.post(url, headers=cabecalho("ana"), json={"para": "pausado", "motivo": "Custo"})
    assert pausa.json()["motivo_status"] == "Custo"
    assert (
        cliente.post(url, headers=cabecalho("ana"), json={"para": "nao_existe"}).status_code == 422
    )


def test_decisao_do_gate_por_papel_e_pessoa(cliente):
    p = novo_projeto()  # M1: responsavel_por_dados = davi
    url = f"/projetos/{p.id}/marcos/M1/decisao"
    corpo = {"decisao": "aprovado", "comentario": "Dados completos"}

    assert cliente.post(url, headers=cabecalho("davi"), json=corpo).status_code == 409  # pendente
    levar_a_gate(p.id, "M1")
    assert cliente.post(url, headers=cabecalho("ana"), json=corpo).status_code == 403  # papel
    assert (
        cliente.post(
            f"/projetos/{p.id}/marcos/M9/decisao", headers=cabecalho("davi"), json=corpo
        ).status_code
        == 404
    )

    r = cliente.post(url, headers=cabecalho("davi"), json=corpo)
    assert r.status_code == 200
    m = r.json()
    assert (m["status"], m["revisor"], m["decisao_gate"]) == ("aprovado", "davi", "Dados completos")
    assert m["decidido_em"] is not None


def test_decisao_em_marco_sem_gate_409_e_reprovar_exige_comentario(cliente):
    p = novo_projeto()
    servico.transicionar(p.id, "em_execucao", "ana")
    servico.atualizar_marco(p.id, "M2", "em_andamento", "sistema")
    sem_gate = cliente.post(
        f"/projetos/{p.id}/marcos/M2/decisao",
        headers=cabecalho("ana"),
        json={"decisao": "aprovado", "comentario": "ok"},
    )
    assert sem_gate.status_code == 409
    levar_a_gate(p.id, "M3")
    sem_comentario = cliente.post(
        f"/projetos/{p.id}/marcos/M3/decisao",
        headers=cabecalho("rita"),
        json={"decisao": "reprovado"},
    )
    assert sem_comentario.status_code == 409
    invalida = cliente.post(
        f"/projetos/{p.id}/marcos/M3/decisao", headers=cabecalho("rita"), json={"decisao": "talvez"}
    )
    assert invalida.status_code == 422


def test_avaliacoes_por_papel_e_nota_valida(cliente):
    p = novo_projeto()
    url = f"/projetos/{p.id}/avaliacoes"
    corpo = {"criterio": "rastreabilidade", "nota": 4, "comentario": "bom"}
    r = cliente.post(url, headers=cabecalho("rita"), json=corpo)
    assert r.status_code == 201
    assert (r.json()["revisor"], r.json()["nota"]) == ("rita", 4)
    assert cliente.post(url, headers=cabecalho("ana"), json=corpo).status_code == 201
    assert cliente.post(url, headers=cabecalho("carla"), json=corpo).status_code == 403
    assert cliente.post(url, headers=cabecalho("rita"), json=corpo | {"nota": 6}).status_code == 422


def test_descarte_de_proximo_passo_so_o_curador_e_do_proprio_projeto(cliente):
    p, outro = novo_projeto(), novo_projeto(titulo="Outro")
    passo = servico.registrar_proximo_passo(p.id, "Estender")
    do_outro = servico.registrar_proximo_passo(outro.id, "Alheio")
    url = f"/projetos/{p.id}/proximos-passos/{passo}/descarte"
    corpo = {"motivo": "Fora de escopo"}

    assert cliente.post(url, headers=cabecalho("ana"), json=corpo).status_code == 403
    cruzado = f"/projetos/{p.id}/proximos-passos/{do_outro}/descarte"
    assert cliente.post(cruzado, headers=cabecalho("carla"), json=corpo).status_code == 404
    assert cliente.post(url, headers=cabecalho("carla"), json={"motivo": " "}).status_code == 409

    r = cliente.post(url, headers=cabecalho("carla"), json=corpo)
    assert r.status_code == 200 and r.json()["descartado_motivo"] == "Fora de escopo"
    assert cliente.post(url, headers=cabecalho("carla"), json=corpo).status_code == 409


def test_conclusao_atalho_em_revisao_para_concluido(cliente):
    p = novo_projeto()
    url = f"/projetos/{p.id}/conclusao"
    assert cliente.post(url, headers=cabecalho("bruno")).status_code == 403
    assert cliente.post(url, headers=cabecalho("ana")).status_code == 409  # aprovado -> concluido

    aprovar_todos_os_gates(p.id)
    ir_para_revisao(p.id)
    sem_licoes = cliente.post(url, headers=cabecalho("ana"))
    assert sem_licoes.status_code == 409 and "lições aprendidas" in sem_licoes.json()["erro"]

    q = novo_projeto(titulo="Completo")
    preparar_fechamento(q.id)
    ok = cliente.post(f"/projetos/{q.id}/conclusao", headers=cabecalho("ana"))
    assert ok.status_code == 200 and ok.json()["status"] == "concluido"


# --- R7: início e retomada da execução (dono), com orçamento opcional --------------------
def test_inicio_da_execucao_so_o_dono_e_so_de_aprovado(cliente):
    from ais4.execucao import orcamento

    p = novo_projeto()
    url = f"/projetos/{p.id}/execucao"
    assert cliente.post(url, headers=cabecalho("bruno")).status_code == 403

    r = cliente.post(url, headers=cabecalho("ana"))

    assert r.status_code == 200 and r.json()["status"] == "em_execucao"
    assert orcamento.obter_ou_padrao(p.id, "ana").max_custo_usd == Decimal("50.00")  # padrão
    assert cliente.post(url, headers=cabecalho("ana")).status_code == 409  # já em execução


def test_inicio_da_execucao_define_o_orcamento(cliente):
    from ais4.execucao import orcamento

    p = novo_projeto()
    corpo = {"max_tokens": 5000, "max_custo_usd": "12.5", "prazo": "2030-01-01T00:00:00Z"}

    r = cliente.post(f"/projetos/{p.id}/execucao", headers=cabecalho("ana"), json=corpo)

    assert r.status_code == 200
    o = orcamento.obter_ou_padrao(p.id, "ana")
    assert (o.max_tokens, o.max_custo_usd) == (5000, Decimal("12.50"))
    assert o.prazo == datetime(2030, 1, 1, tzinfo=UTC)


def test_orcamento_invalido_nao_inicia(cliente):
    p = novo_projeto()
    r = cliente.post(
        f"/projetos/{p.id}/execucao", headers=cabecalho("ana"), json={"max_custo_usd": "0"}
    )
    assert r.status_code in (409, 422)
    assert servico.obter(p.id).status == "aprovado"


def test_retomada_so_de_pausado_e_ajusta_o_orcamento(cliente):
    from ais4.execucao import orcamento

    p = novo_projeto()
    url = f"/projetos/{p.id}/retomada"
    assert cliente.post(url, headers=cabecalho("ana")).status_code == 409  # aprovado
    servico.transicionar(p.id, "em_execucao", "ana")
    servico.transicionar(p.id, "pausado", "ana", "orçamento: custo 50/50")
    assert cliente.post(url, headers=cabecalho("bruno")).status_code == 403

    r = cliente.post(url, headers=cabecalho("ana"), json={"max_custo_usd": "80"})

    assert r.status_code == 200 and r.json()["status"] == "em_execucao"
    o = orcamento.obter_ou_padrao(p.id, "ana")
    assert o.max_custo_usd == Decimal("80.00") and o.max_tokens == 20_000_000  # o resto fica
