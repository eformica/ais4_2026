"""O piloto de ponta a ponta (spec 9): semente -> projeto concluído -> novas ideias no acervo.

Percorre os caminhos reais (rotas HTTP para o que as pessoas fazem; serviços para o que os workers
fazem) com Postgres de verdade e armazém de artefatos em disco. Só o LLM (`ClienteLLMFalso`), os
embeddings (`EmbedderFalso`) e o sandbox Docker (`SandboxGravador`) são falsos: nenhum teste chama a
API real. Cada papel é uma pessoa diferente, e quem pediu o ciclo, quem decide cada gate e quem
escreveu a ideia nunca coincidem (regra de quatro olhos).
"""

import hashlib
import json
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from ais4 import auth, config
from ais4.acervo import servico as acervo
from ais4.acervo.rotas import obter_embedder
from ais4.acervo.similaridade import EmbedderFalso
from ais4.app import criar_app
from ais4.comum import eventos
from ais4.criacao.rotas import obter_cliente_llm
from ais4.criacao.validacao import ResultadoCritica
from ais4.execucao import orcamento, orquestrador
from ais4.execucao.artefatos import Armazem
from ais4.llm.falso import ClienteLLMFalso
from ais4.playbooks import servico as playbooks
from ais4.processos.promocao import elegibilidade
from tests.criacao.fabrica import URL_CENSO, espec_completa
from tests.e2e_helpers import (
    ETAPAS,
    PASSO_DESCARTADO,
    PASSO_IDEIA,
    TITULO_IDEIA_NOVA,
    cabecalho,
    decidir_gates_pendentes,
    drenar_outbox,
    roteiro_da_execucao,
    verificacao,
)
from tests.execucao.test_orquestrador import SandboxGravador
from tests.playbooks.semente import IDEIA_SEMENTE

PESSOAS = {  # uma pessoa por papel da spec 10
    "carla": "curador_do_acervo",
    "ana": "dono_do_projeto",
    "tati": "responsavel_tecnico",
    "davi": "responsavel_por_dados",
    "rita": "revisor_metodologico",
    "fred": "guardiao_de_playbooks",
}
RESPONSAVEIS = {  # quem decide cada gate (R13); o dono entra sozinho como dono_do_projeto
    "responsavel_por_dados": "davi",
    "revisor_metodologico": "rita",
    "curador_do_acervo": "carla",
}
GATES_DECIDIDOS = [("M1", "davi"), ("M3", "rita"), ("M5", "ana"), ("M6", "carla")]
PLAYBOOK = ("projeto-academico-simples", "0.1.0")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def piloto(banco, limpar, tmp_path, monkeypatch):
    """API com as pessoas do piloto, armazém e workspaces em `tmp_path`, LLM e embedder falsos."""
    arquivo = tmp_path / "usuarios.toml"
    arquivo.write_text(
        "\n".join(
            f'[[usuario]]\nnome = "{nome}"\ntoken_sha256 = "{_sha("t-" + nome)}"\n'
            f"papeis = {json.dumps([papel])}\n"
            for nome, papel in PESSOAS.items()
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIS4_USUARIOS_ARQUIVO", str(arquivo))
    monkeypatch.setenv("AIS4_ARTEFATOS_DIR", str(tmp_path / "artefatos"))
    monkeypatch.setenv("AIS4_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()

    fontes = acervo.obter(IDEIA_SEMENTE.id).fontes_candidatas
    llm = ClienteLLMFalso(
        {
            # uma verificação por fonte candidata: só o Censo tem URL (as outras só têm nome)
            "verificador_fontes": [
                verificacao(f["nome"], URL_CENSO if f["nome"] == "IBGE - Censo 2022" else None)
                for f in fontes
            ],
            "redator_projeto": [espec_completa(IDEIA_SEMENTE)],
            "critico_projeto": [ResultadoCritica(lacunas=[])],
            **roteiro_da_execucao(),
        }
    )
    app = criar_app()
    app.dependency_overrides[obter_embedder] = lambda: EmbedderFalso()
    app.dependency_overrides[obter_cliente_llm] = lambda: llm
    with eventos.isolado(vazio=True):
        eventos.carregar_handlers()  # os mesmos handlers do worker-outbox
        yield TestClient(app), llm
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()


def consultar(banco: str, sql: str, params=()):
    with psycopg.connect(banco) as c:
        return c.execute(sql, params).fetchall()


def chamar(api: TestClient, metodo: str, url: str, quem: str, corpo: dict | None = None):
    resposta = api.request(metodo, url, json=corpo, headers=cabecalho(quem))
    assert resposta.status_code in (200, 201), (
        f"{metodo} {url}: {resposta.status_code} {resposta.text}"
    )
    return resposta.json()


def test_ciclo_completo_piloto(piloto, banco):
    api, llm = piloto
    ideia_id = str(IDEIA_SEMENTE.id)

    # 1. Qualificação: a curadora (que não escreveu a ideia) qualifica a semente em triagem
    qualificacao = chamar(api, "POST", f"/acervo/ideias/{ideia_id}/qualificacao", "carla")
    assert qualificacao["qualificada"] is True, qualificacao["pendencias"]
    assert chamar(api, "GET", f"/acervo/ideias/{ideia_id}", "carla")["status"] == "qualificada"

    # 2. Criação: a responsável técnica dispara o ciclo do motor (verifica fontes, redige, critica)
    proposta_id = chamar(api, "POST", "/criacao/ciclos", "tati")["proposta_id"]
    detalhe = chamar(api, "GET", f"/criacao/propostas/{proposta_id}", "ana")
    assert detalhe["proposta"]["status"] == "aguardando_aprovacao"
    assert {f["status"] for f in detalhe["fontes"]} == {"acessivel", "nao_localizada"}

    # 3. Aprovação: quem pediu o ciclo não decide; o dono aprova e designa quem decide cada gate
    decisao = {"acao": "aprovar", "responsaveis": RESPONSAVEIS}
    url_decisao = f"/criacao/propostas/{proposta_id}/decisao"
    assert api.post(url_decisao, json=decisao, headers=cabecalho("tati")).status_code == 403
    projeto_id = chamar(api, "POST", url_decisao, "ana", decisao)["projeto_id"]
    assert chamar(api, "GET", f"/projetos/{projeto_id}", "ana")["status"] == "aprovado"
    drenar_outbox()  # ProjetoCriado -> a ideia fica `em_projeto`
    assert acervo.obter(IDEIA_SEMENTE.id).status == "em_projeto"

    # 4. Início da execução: o dono define o orçamento (R7)
    orcamento_pedido = {"max_tokens": 5_000_000, "max_custo_usd": "25.00"}
    projeto = chamar(api, "POST", f"/projetos/{projeto_id}/execucao", "ana", orcamento_pedido)
    assert projeto["status"] == "em_execucao"
    definido = orcamento.obter_ou_padrao(projeto["id"], "ana")
    assert (definido.max_tokens, definido.max_custo_usd) == (5_000_000, Decimal("25.00"))

    # 5. Execução das 8 etapas pelo worker; cada gate (M1, M3, M5, M6) é decidido pelo responsável
    sandbox, armazem = SandboxGravador(), Armazem()
    decididos = []
    for _ in range(60):
        orquestrador.ciclo(llm, sandbox=sandbox, armazem=armazem)
        decididos += decidir_gates_pendentes(
            api, projeto_id, {**RESPONSAVEIS, "dono_do_projeto": "ana"}, "tati"
        )
        drenar_outbox()
        if chamar(api, "GET", f"/projetos/{projeto_id}", "ana")["status"] == "em_revisao":
            break
    assert decididos == GATES_DECIDIDOS
    marcos = chamar(api, "GET", f"/projetos/{projeto_id}/marcos", "ana")
    assert {m["status"] for m in marcos} == {"aprovado"}
    assert {m["codigo"]: m["revisor"] for m in marcos if m["exige_gate"]} == dict(GATES_DECIDIDOS)
    assert {t["etapa_codigo"]: t["status"] for t in _tarefas(banco, projeto_id)} == {
        e.codigo: "concluida" for e in ETAPAS
    }

    # 6. Retroalimentação (2.2, disparada por `em_revisao`): próximos passos, ideias e lições
    passos = _proximos_passos(banco, projeto_id)
    assert {p[0]: (p[1] is not None, p[2]) for p in passos} == {
        PASSO_IDEIA: (True, None),
        PASSO_DESCARTADO: (False, "Fora do escopo declarado do piloto"),
    }
    derivadas = consultar(
        banco,
        "select titulo, status, origem from acervo.ideia where origem = 'retroalimentacao'",
    )
    assert derivadas == [(TITULO_IDEIA_NOVA, "rascunho", "retroalimentacao")]
    # a lição `sugestao` virou proposta de melhoria do playbook (a `funcionou`, não)
    assert consultar(banco, "select status from playbooks.proposta_melhoria") == [("pendente",)]

    # 7. Conclusão pelo dono; avaliação por quem revisa; o outbox libera a ideia de origem
    assert chamar(api, "POST", f"/projetos/{projeto_id}/conclusao", "ana")["status"] == "concluido"
    avaliacao = {"criterio": "rastreabilidade", "nota": 5, "comentario": "números rastreáveis"}
    chamar(api, "POST", f"/projetos/{projeto_id}/avaliacoes", "rita", avaliacao)
    drenar_outbox()
    assert acervo.obter(IDEIA_SEMENTE.id).status == "concluida"
    promocao = elegibilidade(playbooks.obter_versao(*PLAYBOOK).playbook_id, PLAYBOOK[1])
    assert (promocao.projetos_concluidos, promocao.nota_media, promocao.elegivel) == (1, 5.0, False)

    # --- invariantes do ciclo inteiro ------------------------------------------------------
    # todo artefato registrado confere com o hash guardado no armazém
    artefatos = chamar(api, "GET", f"/projetos/{projeto_id}/artefatos", "ana")
    assert len(artefatos) >= 8
    assert all(armazem.verificar(a["uri"], a["hash_sha256"]) for a in artefatos)

    # `projeto_evento` guarda cada transição, em ordem
    eventos_projeto = consultar(
        banco,
        "select evento, de_status, para_status from projetos.projeto_evento "
        "where projeto_id = %s and evento in ('criado', 'status_alterado') order by id",
        (projeto_id,),
    )
    assert eventos_projeto == [
        ("criado", None, "aprovado"),
        ("status_alterado", "aprovado", "em_execucao"),
        ("status_alterado", "em_execucao", "em_revisao"),
        ("status_alterado", "em_revisao", "concluido"),
    ]

    # o custo registrado chamada a chamada é o que a view `consumo` e o orçamento enxergam
    (soma,) = consultar(
        banco,
        "select sum(custo_usd) from execucao.chamada_llm where projeto_id = %s",
        (projeto_id,),
    )[0]
    (da_view,) = consultar(
        banco, "select custo_usd from execucao.consumo where projeto_id = %s", (projeto_id,)
    )[0]
    assert soma > 0 and soma == da_view == orcamento.consumo(projeto["id"]).custo_usd
    # o gasto do Motor de Criação fica na proposta (sem projeto), fora do consumo do projeto
    (da_proposta,) = consultar(
        banco, "select count(*) from execucao.chamada_llm where proposta_id = %s", (proposta_id,)
    )[0]
    assert da_proposta > 0

    # nada ficou preso no outbox: todo handler passou
    for schema in eventos.SCHEMAS_COM_OUTBOX:
        assert consultar(
            banco, f"select count(*) from {schema}.outbox where publicado_em is null"
        ) == [(0,)]


def _tarefas(banco, projeto_id):
    return [
        {"etapa_codigo": etapa, "status": status}
        for etapa, status in consultar(
            banco,
            "select etapa_codigo, status from execucao.tarefa where projeto_id = %s",
            (projeto_id,),
        )
    ]


def _proximos_passos(banco, projeto_id):
    return consultar(
        banco,
        "select descricao, ideia_gerada_id, descartado_motivo from projetos.proximo_passo "
        "where projeto_id = %s",
        (projeto_id,),
    )
