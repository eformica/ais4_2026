"""Orquestrador do Motor de Execução (Task 15) com `ClienteLLMFalso` e um sandbox falso.

Nenhum teste chama a API real nem o Docker: o `SandboxGravador` interpreta o "script" como um
JSON `{caminho: conteúdo}` e grava esses arquivos no workspace, como um script real faria.
"""

import json
import logging
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import psycopg
import pytest

from ais4 import config
from ais4.comum import eventos
from ais4.comum.erros import RegraViolada
from ais4.execucao import handlers as execucao_handlers
from ais4.execucao import orcamento, orquestrador, servico
from ais4.execucao.artefatos import Armazem
from ais4.execucao.sandbox import ResultadoExec
from ais4.llm.cliente import RespostaInvalida, Uso
from ais4.llm.falso import ClienteLLMFalso
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from ais4.projetos import servico as projetos
from tests.projetos.fabrica import GATES, RESPONSAVEIS, consultar, novo_projeto, usuario

POR_CODIGO = {e.codigo: e for e in ProjetoAcademicoSimples.etapas}
CRITERIOS = [c.codigo for c in ProjetoAcademicoSimples.criterios_qualidade]
CONTEUDO = {  # o que passa em `validar_etapa` (checagens de conteúdo: coleta, modelagem, retroalimentação)
    "catalogo_fontes": json.dumps(
        [{"nome": "Censo Agro", "licenca": "aberta", "cobertura": "Brasil, 2017"}]
    ),
    "cenarios": json.dumps(
        {c: {"premissas": "p", "intervalo": [1, 2]} for c in ("baixo", "medio", "alto")}
    ),
    "novas_ideias": json.dumps(
        [
            {
                "proximo_passo": "Estender a 2060",
                "acao": "descartar",
                "motivo_descarte": "fora do escopo",
            }
        ]
    ),
    "licoes_aprendidas": json.dumps(
        [
            {
                "tipo": "funcionou",
                "descricao": "Harmonizar cedo",
                "evidencia": "etapa de harmonização",
            }
        ]
    ),
}
_REQUISICAO = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


class SandboxGravador:
    """Sandbox falso: o script é um JSON `{caminho: conteúdo}` gravado no workspace."""

    def __init__(self, disponivel: bool = True) -> None:
        self._disponivel = disponivel
        self.chamadas: list[dict] = []

    def disponivel(self) -> bool:
        return self._disponivel

    def executar(self, workspace, script, *, rede, timeout_s=900, semente):
        self.chamadas.append({"workspace": Path(workspace), "rede": rede, "semente": semente})
        pasta = Path(workspace) / "_scripts"
        pasta.mkdir(parents=True, exist_ok=True)
        caminho = pasta / f"_script_{len(list(pasta.iterdir())) + 1}.py"
        caminho.write_text(script, encoding="utf-8")
        arquivos = json.loads(script)
        for relativo, conteudo in arquivos.items():
            destino = Path(workspace) / relativo
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(conteudo, encoding="utf-8")
        return ResultadoExec(
            stdout="ok",
            stderr="",
            codigo_saida=0,
            arquivos_novos=[Path(r) for r in arquivos],
            ambiente={
                "imagem": "ais4-sandbox:0.1.0",
                "imagem_digest": "sha256:falso",
                "semente": semente,
                "rede": rede,
                "seguranca": {"cap_drop": "ALL", "usuario": "1000:1000"},
            },
            script=caminho,
        )


def _arquivos(codigo: str) -> dict[str, tuple[str, str]]:
    """nome da saída -> (arquivo no workspace, conteúdo)."""
    return {
        nome: (
            f"{nome}.json" if nome in CONTEUDO else f"{nome}.md",
            CONTEUDO.get(nome, f"# {nome}\nConteúdo produzido na etapa {codigo}."),
        )
        for nome in POR_CODIGO[codigo].saidas
    }


def entrega(codigo: str, *, python: bool = False, antes=None):
    """Roteiro de agente que grava as saídas da etapa e chama `concluir_etapa`."""
    arquivos = _arquivos(codigo)

    def agente(ferramentas):
        if antes is not None:
            antes(ferramentas)
        if python:
            ferramentas["executar_python"](
                codigo=json.dumps({arquivo: conteudo for arquivo, conteudo in arquivos.values()})
            )
        else:
            for arquivo, conteudo in arquivos.values():
                ferramentas["gravar_arquivo"](caminho=arquivo, conteudo=conteudo)
        retorno = ferramentas["concluir_etapa"](
            artefatos_json=json.dumps({nome: arquivo for nome, (arquivo, _) in arquivos.items()}),
            resumo=f"{codigo} feito",
        )
        assert retorno.startswith("Etapa concluída"), retorno
        return "pronto"

    return agente


def parecer(aprovado: bool, *feedback: str, falhos: tuple[str, ...] = ()) -> str:
    return json.dumps(
        {
            "aprovado": aprovado,
            "feedback": list(feedback),
            "por_criterio": {c: c not in falhos for c in CRITERIOS},
        }
    )


def aprova() -> str:
    return parecer(True)


def reprova(*feedback: str) -> str:
    return parecer(False, *feedback)


def erro_api(classe, status: int, mensagem: str = "falha"):
    return classe(mensagem, response=httpx.Response(status, request=_REQUISICAO), body=None)


@pytest.fixture
def amb(limpar, tmp_path, monkeypatch):
    monkeypatch.setenv("AIS4_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    config.obter_config.cache_clear()
    projeto = novo_projeto()
    projetos.transicionar(projeto.id, "em_execucao", "ana")
    yield SimpleNamespace(
        pid=projeto.id,
        sandbox=SandboxGravador(),
        armazem=Armazem(tmp_path / "armazem"),
        raiz=tmp_path,
    )
    config.obter_config.cache_clear()


def pronta(pid, etapa: str):
    (t,) = [t for t in servico.tarefas(pid) if t.etapa_codigo == etapa and t.status == "pronta"]
    return t


def tentativas(pid, etapa: str):
    return [t for t in servico.tarefas(pid) if t.etapa_codigo == etapa]


def executar(amb, cliente, etapa: str, *, sandbox=None):
    return orquestrador.executar_tarefa(
        pronta(amb.pid, etapa).id, cliente, sandbox=sandbox or amb.sandbox, armazem=amb.armazem
    )


def marco(pid, codigo: str):
    return next(m for m in projetos.marcos(pid) if m.codigo == codigo)


def decidir(pid, codigo: str, decisao: str = "aprovado", comentario: str = "ok"):
    papel = GATES[codigo]
    return projetos.decidir_gate(
        pid, codigo, decisao, comentario, usuario(RESPONSAVEIS[papel], papel)
    )


def concluir_contexto_e_coleta(amb, cliente):
    orquestrador.passo_projeto(amb.pid, cliente)
    assert executar(amb, cliente, "contexto").status == "concluida"
    orquestrador.passo_projeto(amb.pid, cliente)
    return executar(amb, cliente, "coleta_dados")


# --- passo_projeto ---------------------------------------------------------------------------
def test_primeiro_passo_cria_tarefa_contexto(amb):
    cliente = ClienteLLMFalso({})

    criadas = orquestrador.passo_projeto(amb.pid, cliente)

    (t,) = servico.tarefas(amb.pid)
    assert criadas == [t.id]
    assert (t.etapa_codigo, t.tentativa, t.status, t.papel, t.modelo) == (
        "contexto",
        1,
        "pronta",
        "pesquisador",
        "claude-opus-5-5",
    )
    assert t.semente is not None and 0 <= t.semente < 2**31
    assert t.entradas == {"espec_versao": 1, "artefatos": {}}
    assert t.feedback is None
    # idempotente: a etapa já tem tentativa ativa
    assert orquestrador.passo_projeto(amb.pid, cliente) == []
    assert len(servico.tarefas(amb.pid)) == 1
    # projeto fora de execução não recebe tarefas
    parado = novo_projeto(titulo="Outro")
    assert orquestrador.passo_projeto(parado.id, cliente) == []
    assert servico.tarefas(parado.id) == []
    assert cliente.chamadas == []


def test_semente_e_deterministica_por_projeto_etapa_e_tentativa(amb):
    a = orquestrador.semente(amb.pid, "contexto", 1)
    assert a == orquestrador.semente(amb.pid, "contexto", 1)
    assert a != orquestrador.semente(amb.pid, "contexto", 2)
    assert 0 <= a < 2**31
    orquestrador.passo_projeto(amb.pid, None)
    assert pronta(amb.pid, "contexto").semente == a


# --- etapa aprovada, artefatos e marco -------------------------------------------------------
def test_etapa_aprovada_sem_gate_libera_dependente(amb):
    cliente = ClienteLLMFalso({"pesquisador": [entrega("contexto")], "revisor": [aprova()]})
    orquestrador.passo_projeto(amb.pid, cliente)

    final = executar(amb, cliente, "contexto")

    assert final.status == "concluida" and final.finalizado_em is not None
    (art,) = projetos.artefatos(amb.pid)
    assert final.saidas == {"nota_de_contexto": {"uri": art.uri, "sha256": art.hash_sha256}}
    assert (art.nome, art.tipo, art.marco_id) == ("nota_de_contexto", "outro", None)
    assert art.proveniencia == {
        "tarefa_id": str(final.id),
        "etapa": "contexto",
        "tentativa": 1,
        "modelo": "claude-opus-5-5",
        "imagem_digest": None,
        "semente": final.semente,
        "seguranca": None,
        "origem": "saida",
    }
    with amb.armazem.abrir(art.uri) as f:
        assert f.read().decode().startswith("# nota_de_contexto")

    criadas = orquestrador.passo_projeto(amb.pid, cliente)

    coleta = pronta(amb.pid, "coleta_dados")
    assert criadas == [coleta.id]
    assert coleta.entradas["artefatos"] == {
        "nota_de_contexto": {"uri": art.uri, "sha256": art.hash_sha256, "tipo": "outro"}
    }
    assert marco(amb.pid, "M1").status == "em_andamento"
    assert [c.papel for c in cliente.chamadas] == ["pesquisador", "revisor"]


def test_prompt_do_executor_traz_espec_saidas_entradas_e_conclusao(amb):
    lidos = []
    cliente = ClienteLLMFalso(
        {
            "pesquisador": [entrega("contexto")],
            "engenheiro_de_dados": [
                entrega(
                    "coleta_dados",
                    python=True,
                    antes=lambda f: lidos.append(f["ler_artefato"](nome="nota_de_contexto")),
                )
            ],
            "revisor": [aprova(), aprova()],
        }
    )
    concluir_contexto_e_coleta(amb, cliente)

    chamada = next(c for c in cliente.chamadas if c.papel == "engenheiro_de_dados")
    etapa = POR_CODIGO["coleta_dados"]
    assert chamada.sistema == etapa.papel_agente.prompt_base
    espec = projetos.especificacao_vigente(amb.pid)
    for trecho in (
        etapa.criterio_saida,
        "catalogo_fontes",
        "dados_brutos",
        "entradas/nota_de_contexto",
        "concluir_etapa",
        espec.definicao_problema.pergunta_norteadora,
    ):
        assert trecho in chamada.mensagem
    assert "Feedback" not in chamada.mensagem
    assert set(chamada.ferramentas) == {
        "web_fetch",
        "executar_python",
        "gravar_arquivo",
        "ler_artefato",
        "concluir_etapa",
    }
    assert lidos and lidos[0].startswith("# nota_de_contexto")  # entrada disponível ao agente
    assert amb.sandbox.chamadas[0]["rede"] is True


def test_etapa_com_gate_para_ate_decisao(amb):
    cliente = ClienteLLMFalso(
        {
            "pesquisador": [entrega("contexto")],
            "engenheiro_de_dados": [entrega("coleta_dados", python=True)],
            "revisor": [aprova(), aprova()],
        }
    )

    final = concluir_contexto_e_coleta(amb, cliente)

    assert final.status == "concluida"  # R21: a tarefa conclui; o marco carrega o gate
    m1 = marco(amb.pid, "M1")
    assert m1.status == "aguardando_gate"
    assert orquestrador.passo_projeto(amb.pid, cliente) == []
    assert tentativas(amb.pid, "harmonizacao") == []

    arts = {a.nome: a for a in projetos.artefatos(amb.pid) if a.marco_id == m1.id}
    assert set(arts) == {"catalogo_fontes", "dados_brutos", "_script_1.py"}
    assert arts["catalogo_fontes"].tipo == "dataset"
    script = arts["_script_1.py"]
    assert script.tipo == "codigo"
    assert script.proveniencia["origem"] == "script"
    assert script.proveniencia["imagem_digest"] == "sha256:falso"
    assert script.proveniencia["seguranca"] == {"cap_drop": "ALL", "usuario": "1000:1000"}
    assert arts["catalogo_fontes"].proveniencia["imagem_digest"] == "sha256:falso"
    assert final.ambiente["imagem_digest"] == "sha256:falso"
    assert final.ambiente["semente"] == final.semente

    decidir(amb.pid, "M1")
    criadas = orquestrador.passo_projeto(amb.pid, cliente)

    harmonizacao = pronta(amb.pid, "harmonizacao")
    assert criadas == [harmonizacao.id]
    assert set(harmonizacao.entradas["artefatos"]) == {  # ancestrais diretos e transitivos
        "nota_de_contexto",
        "catalogo_fontes",
        "dados_brutos",
    }


# --- falhas de tentativa ---------------------------------------------------------------------
def test_reprovacao_do_revisor_gera_nova_tentativa_com_feedback(amb):
    cliente = ClienteLLMFalso(
        {
            "pesquisador": [entrega("contexto"), entrega("contexto")],
            "revisor": [reprova("Faltam referências verificáveis"), aprova()],
        }
    )
    orquestrador.passo_projeto(amb.pid, cliente)

    primeira = executar(amb, cliente, "contexto")

    assert primeira.status == "falhou" and primeira.erro == "reprovada_pelo_revisor"
    assert primeira.feedback == ["Faltam referências verificáveis"]
    assert primeira.saidas is not None  # as saídas reprovadas ficam registradas na tentativa
    segunda = pronta(amb.pid, "contexto")
    assert segunda.tentativa == 2 and segunda.feedback == ["Faltam referências verificáveis"]
    assert projetos.artefatos(amb.pid) == []  # reprovado não vira artefato do projeto

    final = executar(amb, cliente, "contexto")

    assert "Faltam referências verificáveis" in cliente.chamadas[2].mensagem
    assert final.status == "concluida" and final.tentativa == 2
    assert projetos.obter(amb.pid).status == "em_execucao"


def test_tentativas_esgotadas_pausa_projeto(amb):
    cliente = ClienteLLMFalso(
        {
            "pesquisador": [entrega("contexto")] * 3,
            "revisor": [reprova("ruim 1"), reprova("ruim 2"), reprova("ruim 3")],
        }
    )
    orquestrador.passo_projeto(amb.pid, cliente)

    for _ in range(3):
        executar(amb, cliente, "contexto")

    projeto = projetos.obter(amb.pid)
    assert projeto.status == "pausado"
    assert projeto.motivo_status == "etapa contexto: tentativas esgotadas"
    assert [(t.tentativa, t.status) for t in tentativas(amb.pid, "contexto")] == [
        (1, "falhou"),
        (2, "falhou"),
        (3, "falhou"),
    ]
    assert orquestrador.passo_projeto(amb.pid, cliente) == []


def test_retomada_abre_nova_rodada_de_tentativas(amb):
    cliente = ClienteLLMFalso(
        {
            "pesquisador": [entrega("contexto")] * 4,
            "revisor": [reprova("ruim 1"), reprova("ruim 2"), reprova("ruim 3"), reprova("ruim 4")],
        }
    )
    orquestrador.passo_projeto(amb.pid, cliente)
    for _ in range(3):
        executar(amb, cliente, "contexto")
    assert projetos.obter(amb.pid).status == "pausado"

    projetos.transicionar(amb.pid, "em_execucao", "ana")
    (quarta,) = orquestrador.passo_projeto(amb.pid, cliente)

    t4 = servico.obter_tarefa(quarta)
    assert t4.tentativa == 4 and t4.feedback == ["ruim 3"]
    executar(amb, cliente, "contexto")
    # só uma falha desde a retomada: há nova tentativa e o projeto segue em execução
    assert projetos.obter(amb.pid).status == "em_execucao"
    assert pronta(amb.pid, "contexto").tentativa == 5


@pytest.mark.parametrize(
    ("item", "motivo"),
    [
        (RespostaInvalida("refusal", "fora da política"), "refusal"),
        ("terminei, mas esqueci de concluir", "sem_conclusao"),
    ],
)
def test_refusal_conta_como_falha_de_tentativa(amb, item, motivo):
    cliente = ClienteLLMFalso({"pesquisador": [item]})
    orquestrador.passo_projeto(amb.pid, cliente)

    final = executar(amb, cliente, "contexto")

    assert final.status == "falhou" and final.erro.startswith(f"resposta_invalida: {motivo}")
    segunda = pronta(amb.pid, "contexto")
    assert segunda.tentativa == 2 and any(motivo in f for f in segunda.feedback)
    assert projetos.obter(amb.pid).status == "em_execucao"


def test_revisor_indisponivel_conta_como_reprovacao(amb):
    cliente = ClienteLLMFalso(
        {"pesquisador": [entrega("contexto")], "revisor": [RespostaInvalida("schema", "x")]}
    )
    orquestrador.passo_projeto(amb.pid, cliente)

    final = executar(amb, cliente, "contexto")

    assert final.status == "falhou" and final.feedback == ["revisão indisponível: schema: x"]
    assert pronta(amb.pid, "contexto").tentativa == 2


def test_orcamento_estourado_pausa_e_nao_continua(amb):
    orcamento.definir(amb.pid, 1_000_000, Decimal("1.00"), None, "ana")
    caro = Uso("claude-opus-5-5", 100, 50, 0, 0, Decimal("1.50"), 1, None)
    cliente = ClienteLLMFalso({"pesquisador": [entrega("contexto")]}, uso=caro)
    orquestrador.passo_projeto(amb.pid, cliente)

    final = executar(amb, cliente, "contexto")

    assert final.status == "falhou" and final.erro.startswith("orcamento:")
    (chamada,) = cliente.chamadas
    assert chamada.ferramentas_usadas == []  # a primeira ferramenta já não rodou
    assert not list((amb.raiz / "workspaces").rglob("nota_de_contexto.md"))
    projeto = projetos.obter(amb.pid)
    assert projeto.status == "pausado"
    assert projeto.motivo_status == "orçamento: custo 1.500000/1.00"
    # nada continua: nem nova tentativa, nem nova chamada
    assert orquestrador.ciclo(cliente, sandbox=amb.sandbox, armazem=amb.armazem) is None
    assert len(tentativas(amb.pid, "contexto")) == 1 and len(cliente.chamadas) == 1


def test_orcamento_ja_estourado_nao_chama_o_modelo(amb):
    orcamento.definir(amb.pid, 1_000_000, Decimal("1.00"), None, "ana")
    orcamento.registrar_chamada(
        Uso("claude-opus-5-5", 1, 1, 0, 0, Decimal("2.00"), 1, None), amb.pid, None, None
    )
    cliente = ClienteLLMFalso({})
    orquestrador.passo_projeto(amb.pid, cliente)

    final = executar(amb, cliente, "contexto")

    assert final.status == "falhou" and cliente.chamadas == []
    assert projetos.obter(amb.pid).motivo_status == "orçamento: custo 2.000000/1.00"


def test_erro_transitorio_da_api_nao_conta_no_limite(amb):
    cliente = ClienteLLMFalso(
        {
            "pesquisador": [
                erro_api(anthropic.RateLimitError, 429, "devagar"),
                entrega("contexto"),
                anthropic.APIConnectionError(request=_REQUISICAO),
                entrega("contexto"),
                erro_api(anthropic.OverloadedError, 529, "sobrecarga"),
                entrega("contexto"),
            ],
            "revisor": [reprova("r1"), reprova("r2"), reprova("r3")],
        }
    )
    orquestrador.passo_projeto(amb.pid, cliente)

    primeira = executar(amb, cliente, "contexto")
    assert primeira.status == "falhou" and primeira.erro.startswith("api_transitoria: ")
    for _ in range(4):
        executar(amb, cliente, "contexto")
    assert projetos.obter(amb.pid).status == "em_execucao"  # 2 reprovações + 3 transitórias

    executar(amb, cliente, "contexto")

    projeto = projetos.obter(amb.pid)
    assert projeto.status == "pausado"
    assert projeto.motivo_status == "etapa contexto: tentativas esgotadas"
    assert len(tentativas(amb.pid, "contexto")) == 6


def test_feedback_sobrevive_a_falha_transitoria(amb):
    cliente = ClienteLLMFalso(
        {
            "pesquisador": [entrega("contexto"), erro_api(anthropic.InternalServerError, 500)],
            "revisor": [reprova("cite as fontes")],
        }
    )
    orquestrador.passo_projeto(amb.pid, cliente)
    executar(amb, cliente, "contexto")
    executar(amb, cliente, "contexto")
    assert pronta(amb.pid, "contexto").feedback == ["cite as fontes"]


def test_erro_sistemico_da_api_pausa_o_projeto(amb):
    cliente = ClienteLLMFalso(
        {"pesquisador": [erro_api(anthropic.AuthenticationError, 401, "chave inválida")]}
    )
    orquestrador.passo_projeto(amb.pid, cliente)

    final = executar(amb, cliente, "contexto")

    assert final.status == "falhou" and "chave inválida" in final.erro
    projeto = projetos.obter(amb.pid)
    assert projeto.status == "pausado"
    assert projeto.motivo_status.startswith("erro sistêmico da API: ")
    assert "chave inválida" in projeto.motivo_status
    assert len(tentativas(amb.pid, "contexto")) == 1


def test_erro_inesperado_conta_como_falha_e_nao_derruba(amb):
    def quebra(_ferramentas):
        raise ZeroDivisionError("bug")

    cliente = ClienteLLMFalso({"pesquisador": [quebra]})
    orquestrador.passo_projeto(amb.pid, cliente)

    final = executar(amb, cliente, "contexto")

    assert final.status == "falhou" and final.erro.startswith("erro_interno: ZeroDivisionError")
    assert pronta(amb.pid, "contexto").tentativa == 2


def test_interrupcao_marca_falhou_e_relanca(amb):
    cliente = ClienteLLMFalso({"pesquisador": [KeyboardInterrupt()]})
    orquestrador.passo_projeto(amb.pid, cliente)
    t = pronta(amb.pid, "contexto")

    with pytest.raises(KeyboardInterrupt):
        executar(amb, cliente, "contexto")

    final = servico.obter_tarefa(t.id)
    assert (final.status, final.erro) == ("falhou", "interrompido")
    # a próxima volta do orquestrador abre a nova tentativa
    (nova,) = orquestrador.passo_projeto(amb.pid, cliente)
    assert servico.obter_tarefa(nova).tentativa == 2


# --- lease (Review Focus 1) -----------------------------------------------------------------
def _vencer_lease(banco, tarefa_id):
    with psycopg.connect(banco, autocommit=True) as c:
        c.execute(
            "update execucao.tarefa set lease_ate = now() - interval '1 minute' where id = %s",
            (tarefa_id,),
        )


def test_lease_expirado_gera_nova_tentativa(amb, banco):
    orquestrador.passo_projeto(amb.pid, None)
    t = pronta(amb.pid, "contexto")
    assert servico.reservar(t.id, orquestrador.LEASE_S)
    assert orquestrador.recuperar_expiradas() == []  # lease em dia: nada a fazer
    _vencer_lease(banco, t.id)

    assert orquestrador.recuperar_expiradas() == [t.id]

    morta = servico.obter_tarefa(t.id)
    assert (morta.status, morta.erro) == ("falhou", "lease_expirado")
    assert pronta(amb.pid, "contexto").tentativa == 2
    # o worker zumbi não consegue sobrescrever o registro (R21)
    with pytest.raises(RegraViolada):
        servico.finalizar(t.id, "validando")
    with pytest.raises(RegraViolada):
        servico.finalizar(t.id, "falhou", erro="zumbi")
    assert servico.obter_tarefa(t.id).erro == "lease_expirado"
    assert orquestrador.recuperar_expiradas() == []


def test_lease_expirado_em_validacao_tambem_e_recuperado(amb, banco):
    orquestrador.passo_projeto(amb.pid, None)
    t = pronta(amb.pid, "contexto")
    assert servico.reservar(t.id, orquestrador.LEASE_S)
    servico.finalizar(t.id, "validando")
    _vencer_lease(banco, t.id)

    assert orquestrador.recuperar_expiradas() == [t.id]
    assert servico.obter_tarefa(t.id).erro == "lease_expirado"
    assert pronta(amb.pid, "contexto").tentativa == 2


def test_queda_do_worker_nao_conta_como_falha_do_agente(amb, banco):
    """Revisão final #8: lease_expirado e interrompido não esgotam as tentativas da etapa."""
    orquestrador.passo_projeto(amb.pid, None)
    for _ in range(3):
        t = pronta(amb.pid, "contexto")
        assert servico.reservar(t.id, orquestrador.LEASE_S)
        _vencer_lease(banco, t.id)
        assert orquestrador.recuperar_expiradas() == [t.id]
    cliente = ClienteLLMFalso({"pesquisador": [KeyboardInterrupt()]})
    with pytest.raises(KeyboardInterrupt):
        executar(amb, cliente, "contexto")
    orquestrador.passo_projeto(amb.pid, cliente)

    assert projetos.obter(amb.pid).status == "em_execucao"
    assert [t.erro for t in tentativas(amb.pid, "contexto")][:4] == [
        "lease_expirado",
        "lease_expirado",
        "lease_expirado",
        "interrompido",
    ]
    assert pronta(amb.pid, "contexto").tentativa == 5


def test_worker_que_perdeu_o_lease_para_sem_sobrescrever(amb, banco):
    def varrido_no_meio(_ferramentas):
        _vencer_lease(banco, pronta_antes.id)
        orquestrador.recuperar_expiradas()

    cliente = ClienteLLMFalso({"pesquisador": [entrega("contexto", antes=varrido_no_meio)]})
    orquestrador.passo_projeto(amb.pid, cliente)
    pronta_antes = pronta(amb.pid, "contexto")

    executar(amb, cliente, "contexto")

    morta = servico.obter_tarefa(pronta_antes.id)
    assert (morta.status, morta.erro) == ("falhou", "lease_expirado")
    assert cliente.chamadas[0].ferramentas_usadas == []  # parou na primeira mensagem
    assert [t.status for t in tentativas(amb.pid, "contexto")] == ["falhou", "pronta"]


# --- Docker indisponível (R24) ---------------------------------------------------------------
def test_docker_indisponivel_nao_reserva_etapa_com_python(amb, caplog):
    harmonizacao = servico.criar_tarefa(amb.pid, "harmonizacao", "engenheiro_de_dados", "m", {}, 1)
    sem_docker = SandboxGravador(disponivel=False)
    cliente = ClienteLLMFalso({"pesquisador": [entrega("contexto")], "revisor": [aprova()]})

    with caplog.at_level(logging.WARNING):
        executada = orquestrador.ciclo(cliente, sandbox=sem_docker, armazem=amb.armazem)

    contexto = tentativas(amb.pid, "contexto")[0]
    assert executada == contexto.id and contexto.status == "concluida"
    assert servico.obter_tarefa(harmonizacao.id).status == "pronta"
    assert "Docker" in caplog.text
    assert (
        orquestrador.executar_tarefa(
            harmonizacao.id, cliente, sandbox=sem_docker, armazem=amb.armazem
        )
        is None
    )
    assert servico.obter_tarefa(harmonizacao.id).status == "pronta"


# --- gates, pausa e término -----------------------------------------------------------------
def test_gate_reprovado_reabre_etapa_com_feedback(amb):
    cliente = ClienteLLMFalso(
        {
            "pesquisador": [entrega("contexto")],
            "engenheiro_de_dados": [entrega("coleta_dados", python=True)] * 2,
            "revisor": [aprova()] * 3,
        }
    )
    concluir_contexto_e_coleta(amb, cliente)
    decidir(amb.pid, "M1", "reprovado", "Falta a licença do Censo")

    with eventos.isolado(vazio=True):
        execucao_handlers.registrar()
        eventos.publicar_pendentes("projetos")

    assert marco(amb.pid, "M1").status == "em_andamento"
    segunda = pronta(amb.pid, "coleta_dados")
    assert segunda.tentativa == 2 and segunda.feedback == ["Falta a licença do Censo"]
    assert orquestrador.ao_reprovar_gate(amb.pid, "M1") is None  # idempotente
    assert len(tentativas(amb.pid, "coleta_dados")) == 2

    final = executar(amb, cliente, "coleta_dados")

    assert "Falta a licença do Censo" in cliente.chamadas[-2].mensagem
    assert final.status == "concluida"
    assert marco(amb.pid, "M1").status == "aguardando_gate"


@pytest.mark.parametrize("para", ["pausado", "cancelado"])
def test_pausa_ou_cancelamento_cancela_tarefas_prontas(amb, para):
    orquestrador.passo_projeto(amb.pid, None)
    t = pronta(amb.pid, "contexto")
    projetos.transicionar(amb.pid, para, "ana", "decisão do dono")

    with eventos.isolado(vazio=True):
        execucao_handlers.registrar()
        eventos.publicar_pendentes("projetos")
        assert servico.obter_tarefa(t.id).status == "cancelada"
        servico.cancelar_abertas(amb.pid)  # de novo: nada muda, nada quebra
    assert servico.obter_tarefa(t.id).status == "cancelada"

    if para == "pausado":  # a retomada recria a tentativa cancelada
        projetos.transicionar(amb.pid, "em_execucao", "ana")
        (nova,) = orquestrador.passo_projeto(amb.pid, None)
        assert servico.obter_tarefa(nova).tentativa == 2


def test_handlers_de_execucao_sao_carregados():
    assert "ais4.execucao.handlers" in eventos.MODULOS_COM_HANDLERS


def test_ultima_etapa_aprovada_move_para_em_revisao(amb):
    com_python = {"coleta_dados", "harmonizacao", "analise_exploratoria", "modelagem", "relatorio"}
    roteiro: dict[str, list] = {"revisor": [aprova()] * 8}
    for etapa in ProjetoAcademicoSimples.etapas:
        roteiro.setdefault(etapa.papel_agente.papel, []).append(
            entrega(etapa.codigo, python=etapa.codigo in com_python)
        )
    cliente = ClienteLLMFalso(roteiro)

    for _ in range(30):
        orquestrador.ciclo(cliente, sandbox=amb.sandbox, armazem=amb.armazem)
        for m in projetos.marcos(amb.pid):
            if m.status == "aguardando_gate":
                decidir(amb.pid, m.codigo)
        if projetos.obter(amb.pid).status != "em_execucao":
            break

    assert projetos.obter(amb.pid).status == "em_revisao"
    assert {t.etapa_codigo: t.status for t in servico.tarefas(amb.pid)} == {
        e.codigo: "concluida" for e in ProjetoAcademicoSimples.etapas
    }
    assert {m.status for m in projetos.marcos(amb.pid)} == {"aprovado"}
    relatorio = next(a for a in projetos.artefatos(amb.pid) if a.nome == "relatorio")
    assert relatorio.tipo == "relatorio"


def test_loop_roda_ciclos_ate_parar_e_sobrevive_a_erro(amb, monkeypatch):
    parar = threading.Event()
    voltas = []

    def ciclo_falso(cliente, *, sandbox, armazem):
        voltas.append((cliente, sandbox, armazem))
        if len(voltas) == 1:
            raise RuntimeError("falha de um ciclo")
        parar.set()

    monkeypatch.setattr(orquestrador, "ciclo", ciclo_falso)

    orquestrador.loop("cliente", 0.01, parar, sandbox=amb.sandbox, armazem=amb.armazem)

    assert voltas == [("cliente", amb.sandbox, amb.armazem)] * 2


# --- projeto parado no meio da tentativa (fix round 1) --------------------------------------
def publicar_eventos_de_projetos():
    with eventos.isolado(vazio=True):
        execucao_handlers.registrar()
        eventos.publicar_pendentes("projetos")


def chamadas_llm(banco, tarefa_id) -> int:
    return consultar(
        banco, "select count(*) from execucao.chamada_llm where tarefa_id = %s", (tarefa_id,)
    )[0][0]


@pytest.mark.parametrize("para", ["pausado", "cancelado"])
@pytest.mark.parametrize("publicar", [True, False], ids=["com_handler", "outbox_atrasado"])
def test_projeto_parado_no_meio_interrompe_a_tentativa_sem_gravar(amb, banco, para, publicar):
    def parar(_ferramentas):
        projetos.transicionar(amb.pid, para, "ana", "decisão do dono")
        if publicar:
            publicar_eventos_de_projetos()

    cliente = ClienteLLMFalso(
        {"pesquisador": [entrega("contexto", antes=parar)], "revisor": [aprova()]}
    )
    orquestrador.passo_projeto(amb.pid, cliente)
    t = pronta(amb.pid, "contexto")

    final = executar(amb, cliente, "contexto")

    assert (final.status, final.erro, final.lease_ate) == ("cancelada", None, None)
    (chamada,) = cliente.chamadas  # nenhuma chamada ao revisor
    assert chamada.ferramentas_usadas == []  # parou na primeira mensagem depois da parada
    assert chamadas_llm(banco, t.id) == 1  # e nenhuma mensagem a mais foi paga
    assert not list((amb.raiz / "workspaces").rglob("nota_de_contexto.md"))
    assert projetos.artefatos(amb.pid) == []
    assert projetos.obter(amb.pid).status == para
    assert len(tentativas(amb.pid, "contexto")) == 1


def test_pausa_durante_a_revisao_nao_registra_nada(amb):
    class PausaNaRevisao(ClienteLLMFalso):
        def estruturado(self, **kw):
            parecer_ = super().estruturado(**kw)
            projetos.transicionar(amb.pid, "pausado", "ana", "pausa no meio da revisão")
            return parecer_

    cliente = PausaNaRevisao({"pesquisador": [entrega("contexto")], "revisor": [aprova()]})
    orquestrador.passo_projeto(amb.pid, cliente)

    final = executar(amb, cliente, "contexto")

    assert final.status == "cancelada" and final.erro is None
    assert projetos.artefatos(amb.pid) == []
    assert projetos.obter(amb.pid).status == "pausado"
    # a retomada refaz a etapa do zero, em nova tentativa
    projetos.transicionar(amb.pid, "em_execucao", "ana")
    (nova,) = orquestrador.passo_projeto(amb.pid, cliente)
    assert servico.obter_tarefa(nova).tentativa == 2


def test_finalizar_de_zumbi_e_tarefa_perdida_nao_erro_interno(amb, banco, monkeypatch, caplog):
    def cancelar_por_fora(_ferramentas):
        with psycopg.connect(banco, autocommit=True) as c:
            c.execute("update execucao.tarefa set status = 'cancelada' where id = %s", (alvo.id,))

    monkeypatch.setattr(orquestrador.servico, "renovar_lease", lambda *_a: True)  # não percebe
    cliente = ClienteLLMFalso(
        {"pesquisador": [entrega("contexto", antes=cancelar_por_fora)], "revisor": [aprova()]}
    )
    orquestrador.passo_projeto(amb.pid, cliente)
    alvo = pronta(amb.pid, "contexto")

    with caplog.at_level(logging.INFO):
        final = executar(amb, cliente, "contexto")

    assert (final.status, final.erro) == ("cancelada", None)  # o registro de quem cancelou fica
    assert len(cliente.chamadas) == 1  # sem revisor
    assert len(tentativas(amb.pid, "contexto")) == 1  # nada contado, nenhuma tentativa nova
    assert "Erro inesperado" not in caplog.text and "Traceback" not in caplog.text
    assert "abandonada" in caplog.text


def test_lease_renovado_em_volta_de_script_longo(amb, banco):
    def restante_s(tarefa_id) -> float:
        return consultar(
            banco,
            "select extract(epoch from lease_ate - now()) from execucao.tarefa where id = %s",
            (tarefa_id,),
        )[0][0]

    class SandboxLento(SandboxGravador):
        def executar(self, workspace, script, **kw):
            antes.append(restante_s(alvo.id))
            with psycopg.connect(banco, autocommit=True) as c:  # 29 minutos de script
                c.execute(
                    "update execucao.tarefa set lease_ate = now() + interval '1 minute' "
                    "where id = %s",
                    (alvo.id,),
                )
            return super().executar(workspace, script, **kw)

    antes, depois = [], []

    def agente(ferramentas):
        ferramentas["executar_python"](codigo=json.dumps({"dados.csv": "a\n1\n"}))
        depois.append(restante_s(alvo.id))  # antes de qualquer nova mensagem do modelo
        return "sem concluir"

    alvo = servico.criar_tarefa(amb.pid, "harmonizacao", "engenheiro_de_dados", "m", {}, 1)
    cliente = ClienteLLMFalso({"engenheiro_de_dados": [agente]})

    orquestrador.executar_tarefa(alvo.id, cliente, sandbox=SandboxLento(), armazem=amb.armazem)

    assert orquestrador.LEASE_S == 1800
    assert antes[0] > 1700 and depois[0] > 1700  # renovado antes e depois do script
