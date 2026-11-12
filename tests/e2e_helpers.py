"""Apoio do teste ponta a ponta do piloto: roteiros do LLM falso e ações dos humanos (via API).

O LLM falso é uma só instância com um roteiro por papel (o Motor de Criação usa `verificador_fontes`,
`redator_projeto` e `critico_projeto`; a execução, o papel de cada etapa do playbook e `revisor`).
"""

import json

from fastapi.testclient import TestClient

from ais4.comum import eventos
from ais4.execucao.ferramentas import usa_sandbox
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from tests.execucao.test_orquestrador import CONTEUDO as CONTEUDO_PADRAO
from tests.execucao.test_orquestrador import POR_CODIGO, aprova

ETAPAS = list(ProjetoAcademicoSimples.etapas)

# Formatos do processo 2.2 (R27, `ais4.comum.retroalimentacao`): uma ideia nova e um descarte.
PASSO_IDEIA = "Estender as projeções de hortifruti para microrregiões"
PASSO_DESCARTADO = "Simular políticas públicas de abastecimento"
TITULO_IDEIA_NOVA = "Hortifruti por microrregião: oferta e demanda até 2050"
NOVAS_IDEIAS = [
    {
        "proximo_passo": PASSO_IDEIA,
        "acao": "ideia",
        "ideia": {
            "titulo": TITULO_IDEIA_NOVA,
            "tipo": "pergunta_de_pesquisa",
            "descricao": "Repetir a análise de saldo regional para perecíveis em escala menor.",
            "pergunta_norteadora": "Quais microrregiões ficam deficitárias em hortifruti até 2050?",
            "dominio": ["agro", "logistica"],
            "valor": 4,
            "viabilidade": 3,
        },
    },
    {
        "proximo_passo": PASSO_DESCARTADO,
        "acao": "descartar",
        "motivo_descarte": "Fora do escopo declarado do piloto",
    },
]
LICOES = [
    {
        "tipo": "funcionou",
        "descricao": "Harmonizar as séries antes de projetar",
        "evidencia": "etapa harmonizacao",
    },
    {
        "tipo": "sugestao",
        "descricao": "Pedir a licença de uso dos dados já na coleta",
        "evidencia": "gate M1",
    },
]
CONTEUDO = {
    **CONTEUDO_PADRAO,
    "novas_ideias": json.dumps(NOVAS_IDEIAS, ensure_ascii=False),
    "licoes_aprendidas": json.dumps(LICOES, ensure_ascii=False),
}


def cabecalho(nome: str) -> dict[str, str]:
    return {"Authorization": f"Bearer t-{nome}"}


def verificacao(nome: str, url: str | None):
    """Roteiro do verificador: fonte com URL testada é `acessivel`; sem URL, `nao_localizada`."""

    def roteiro(ferramentas):
        ferramentas["registrar_verificacao"](
            nome=nome,
            url=url,
            status="acessivel" if url else "nao_localizada",
            cobertura=json.dumps({"temporal": "2022", "licenca": "CC BY 4.0"}) if url else "{}",
            evidencia="web_fetch abriu a página." if url else "Nenhuma página oficial achada.",
        )
        return "registrado"

    return roteiro


def entrega(codigo: str):
    """Roteiro do agente de uma etapa: grava as saídas (via sandbox ou `gravar_arquivo`) e conclui."""
    etapa = POR_CODIGO[codigo]
    arquivos = {
        nome: (
            f"{nome}.json" if nome in CONTEUDO else f"{nome}.md",
            CONTEUDO.get(nome, f"# {nome}\nConteúdo produzido na etapa {codigo}."),
        )
        for nome in etapa.saidas
    }

    def agente(ferramentas):
        if usa_sandbox(etapa):
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


def roteiro_da_execucao() -> dict[str, list]:
    """Um agente por etapa (no papel dela) e um parecer aprovado do revisor para cada uma."""
    roteiro: dict[str, list] = {"revisor": [aprova() for _ in ETAPAS]}
    for etapa in ETAPAS:
        roteiro.setdefault(etapa.papel_agente.papel, []).append(entrega(etapa.codigo))
    return roteiro


def drenar_outbox() -> None:
    """O que o `worker-outbox` faz em loop: entrega os eventos pendentes de cada schema, até que
    nenhum handler gere mais eventos (um handler de um schema grava no outbox de outro)."""
    while sum(eventos.publicar_pendentes(schema) for schema in eventos.SCHEMAS_COM_OUTBOX):
        pass


def decidir_gates_pendentes(
    api: TestClient, projeto_id, responsaveis: dict[str, str], intruso: str
) -> list[tuple[str, str]]:
    """Decide (aprovando) cada marco `aguardando_gate`, pela pessoa designada ao papel do gate.

    Antes da pessoa certa, `intruso` (outra pessoa, sem o papel) tenta decidir e leva 403.
    Devolve `[(codigo do marco, quem decidiu)]`.
    """
    decididos = []
    for marco in api.get(f"/projetos/{projeto_id}/marcos", headers=cabecalho(intruso)).json():
        if marco["status"] != "aguardando_gate":
            continue
        url = f"/projetos/{projeto_id}/marcos/{marco['codigo']}/decisao"
        corpo = {"decisao": "aprovado", "comentario": f"{marco['codigo']} conferido"}
        assert api.post(url, json=corpo, headers=cabecalho(intruso)).status_code == 403
        pessoa = responsaveis[marco["papel_revisor"]]
        resposta = api.post(url, json=corpo, headers=cabecalho(pessoa))
        assert resposta.status_code == 200, resposta.text
        decididos.append((marco["codigo"], pessoa))
    return decididos
