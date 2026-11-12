"""Playbook semente "Projeto Acadêmico Simples" v0.1.0 (spec 6.3).

Etapas, critérios, pré-requisitos e entregáveis são idênticos a `db/sql/04_seed.sql`
(`servico.sincronizar` recusa qualquer divergência). Os `prompt_base` vivem só aqui (R5).
"""

import json
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ais4.comum.dto import ArtefatoRef, IdeiaDTO
from ais4.comum.espec import DefinicaoProblema, EspecProjeto, Fonte, MarcoPlanejado, Metodologia
from ais4.comum.retroalimentacao import ler_licoes, ler_novas_ideias
from ais4.playbooks.base import (
    Avaliacao,
    Criterio,
    Entregavel,
    Etapa,
    PapelAgente,
    Playbook,
    Requisito,
    Resultado,
    SaidaEtapa,
)
from ais4.playbooks.registro import registrar

TIPOS_COMPATIVEIS = frozenset({"pergunta_de_pesquisa", "problema", "hipotese"})
FATOR_TIPO_INCOMPATIVEL = 0.5
# Pré-requisitos que a ideia sozinha comprova; o de "ambiente" é garantido pelo sandbox.
TOTAL_PRE_REQUISITOS = 3

CENARIOS_OBRIGATORIOS = ("baixo", "medio", "alto")

# Marco -> etapa, em ordem. Os marcos das etapas com gate vêm da própria etapa.
MARCOS = (
    ("M1", "coleta_dados"),
    ("M2", "analise_exploratoria"),
    ("M3", "modelagem"),
    ("M4", "sintese"),
    ("M5", "relatorio"),
    ("M6", "retroalimentacao"),
)


def _prompt(abertura: str, tarefas: list[str], saidas: dict[str, str], criterios: list[str]) -> str:
    """Monta o prompt de uma etapa; as saídas citadas são as do `saidas` da etapa."""
    linhas = [abertura, "", "O que fazer:", *(f"- {t}" for t in tarefas), ""]
    linhas += [
        "Saídas esperadas: grave cada uma como arquivo e entregue todas de uma vez à ferramenta",
        "`concluir_etapa`, usando exatamente estes nomes:",
        *(f"- `{nome}`: {descricao}" for nome, descricao in saidas.items()),
        "",
        "Critérios de qualidade desta etapa:",
        *(f"- {c}" for c in criterios),
        "",
        "Se algo não puder ser cumprido, diga no resumo de `concluir_etapa`; nunca invente dados.",
    ]
    return "\n".join(linhas)


_PROMPT_CONTEXTO = _prompt(
    "Você é o pesquisador do projeto: levanta o contexto e a literatura necessários para "
    "responder à pergunta norteadora da especificação.",
    [
        (
            "Leia a especificação (problema, escopo, hipóteses) e liste os conceitos e as "
            "definições operacionais que precisam ser fixados."
        ),
        (
            "Busque com `web_search` e abra com `web_fetch` fontes primárias e oficiais: artigos, "
            "relatórios técnicos, documentação das bases de dados."
        ),
        "Resuma o estado do conhecimento, as lacunas e como tudo se relaciona ao escopo.",
        "Registre, para cada referência, título, autoria ou instituição, ano e a URL aberta.",
    ],
    {"nota_de_contexto": "nota em Markdown com o resumo do contexto e a lista de referências"},
    [
        (
            "Referências verificáveis: cite só o que você abriu; toda afirmação numérica leva a "
            "sua fonte."
        ),
        "Limitações declaradas: aponte o que procurou e não encontrou.",
    ],
)

_PROMPT_COLETA = _prompt(
    "Você é o engenheiro de dados responsável por inventariar e coletar os dados do projeto. "
    "A rede só está disponível nesta etapa.",
    [
        (
            "Para cada item de `fontes_dados` da especificação, localize a URL oficial se ela "
            "faltar e teste o acesso."
        ),
        "Registre cobertura (temporal e geográfica), periodicidade, licença e metadados.",
        (
            "Baixe os dados com `python` e os mantenha como vieram, sem transformá-los; guarde "
            "URL de origem, data da coleta e hash."
        ),
        (
            "Fonte inacessível ou não localizada: registre o status, sem trocá-la por outra "
            "em silêncio."
        ),
    ],
    {
        "catalogo_fontes": "JSON com uma lista de objetos, um por fonte, com `nome`, `url`, "
        "`licenca`, `cobertura`, `periodicidade` e `status_acesso`; `licenca` e `cobertura` "
        "são obrigatórias em todo item",
        "dados_brutos": "arquivos baixados sem alteração, com os metadados de origem",
    },
    [
        "Rastreabilidade: todo dado liga-se a uma fonte catalogada.",
        "Reprodutibilidade: salve os scripts de download para reexecutar do zero.",
        "Limitações declaradas: documente lacunas de cobertura e restrições de licença.",
    ],
)

_PROMPT_HARMONIZACAO = _prompt(
    "Você é o engenheiro de dados que transforma os dados brutos em um dataset analítico "
    "confiável. Não há acesso à rede nesta etapa.",
    [
        (
            "Carregue `dados_brutos` e padronize nomes, tipos, unidades e códigos territoriais "
            "(por exemplo, código IBGE)."
        ),
        "Trate ausentes, duplicatas e inconsistências com regras explícitas e justificadas.",
        "Escreva o tratamento como script reexecutável do zero, com sementes fixas.",
        "Registre cada transformação: o que mudou, por quê e quantas linhas foram afetadas.",
    ],
    {
        "dataset_analitico": "tabela final (CSV ou Parquet) pronta para análise",
        "dicionario_dados": "uma entrada por coluna: nome, descrição, unidade, tipo e fonte",
        "log_tratamento": "registro ordenado de todas as transformações aplicadas",
    },
    [
        "Rastreabilidade: cada coluna do dataset remete à fonte de origem no dicionário.",
        "Reprodutibilidade: o script refaz o dataset a partir dos dados brutos.",
        "Limitações declaradas: anote decisões de tratamento que afetem as conclusões.",
    ],
)

_PROMPT_EXPLORATORIA = _prompt(
    "Você é o analista que explora o dataset analítico e descreve o que os dados mostram.",
    [
        (
            "Calcule estatísticas descritivas das variáveis do escopo: tendência, dispersão, "
            "ausentes e valores atípicos, por região e período."
        ),
        "Produza gráficos com eixos, unidades e fonte rotulados.",
        "Cubra todas as variáveis e recortes do escopo, não só os mais fáceis.",
        "Use `python` com sementes fixas e guarde o código das análises.",
    ],
    {
        "estatisticas_descritivas": "tabelas (CSV ou JSON) com as estatísticas por variável",
        "graficos": "imagens (PNG ou SVG) com legenda de cada gráfico",
    },
    [
        "Rastreabilidade: cada número e gráfico cita o dataset e a coluna de origem.",
        "Observado x projetado: se houver séries projetadas, distinga-as visualmente.",
        "Reprodutibilidade: o código regenera tudo a partir do dataset analítico.",
    ],
)

_PROMPT_MODELAGEM = _prompt(
    "Você é o modelador que projeta as séries principais do projeto por cenários. "
    "Cenários não são previsões e devem ser tratados assim.",
    [
        "Ajuste modelos (`statsmodels`, `scikit-learn`) às séries do escopo com sementes fixas.",
        "Construa os cenários `baixo`, `medio` e `alto`, cada um com premissas explícitas.",
        "Calcule o intervalo de incerteza de cada cenário; projeção sem intervalo não vale.",
        "Meça o erro com validação temporal (backtesting), por exemplo MAE e MAPE.",
    ],
    {
        "modelos": "especificação e parâmetros dos modelos ajustados, com o código",
        "cenarios": "JSON com as chaves `baixo`, `medio` e `alto`; cada uma traz `premissas` "
        "e `intervalo` (limites inferior e superior), obrigatório, além das séries projetadas",
        "metricas_erro": "tabela com as métricas de erro por modelo e série",
    },
    [
        "Incerteza: toda projeção com intervalo e premissas explícitas.",
        "Observado x projetado: distinga os dois em todos os gráficos e tabelas.",
        "Reprodutibilidade: o código reexecuta do zero e reproduz os resultados.",
    ],
)

_PROMPT_SINTESE = _prompt(
    "Você é o analista que interpreta os resultados das etapas anteriores. Não há "
    "ferramentas de execução: trabalhe com o que já foi produzido.",
    [
        "Leia os artefatos das etapas anteriores e responda à pergunta norteadora.",
        (
            "Para cada hipótese de trabalho, diga se os resultados a sustentam, refutam ou são "
            "inconclusivos."
        ),
        "Ligue cada achado à evidência: artefato e número que o sustentam.",
        "Trate as projeções como cenários e declare o que os resultados não permitem concluir.",
    ],
    {
        "achados": "lista de achados, cada um com a evidência que o sustenta",
        "limitacoes": "limitações de dados, método e escopo que afetam a interpretação",
    },
    [
        "Rastreabilidade: nenhum achado sem evidência indicada.",
        "Limitações declaradas: explícitas e específicas, não genéricas.",
        "Incerteza: não afirme além dos intervalos e das premissas dos cenários.",
    ],
)

_PROMPT_RELATORIO = _prompt(
    "Você é o redator que transforma a síntese em relatório e apresentação para o público "
    "do projeto.",
    [
        (
            "Escreva o relatório com insights, metodologia, resultados, discussão, limitações e "
            "próximos passos, mais as referências."
        ),
        "Monte uma apresentação enxuta com os achados principais.",
        "Mantenha dados observados e projetados sempre distinguidos nos gráficos e tabelas.",
        "Escreva próximos passos concretos: eles viram ideias na etapa de retroalimentação.",
    ],
    {
        "relatorio": "documento completo com todas as seções acima",
        "apresentacao": "slides com os principais achados, a metodologia e os próximos passos",
    },
    [
        "Rastreabilidade: cada afirmação numérica cita a fonte ou o artefato de origem.",
        "Observado x projetado: distinção visual em todo gráfico.",
        "Incerteza: intervalos e premissas dos cenários sempre visíveis.",
        "Limitações declaradas e metodologia descrita de modo que outra pessoa a reproduza.",
    ],
)

_PROMPT_RETROALIMENTACAO = _prompt(
    "Você é o curador que fecha o ciclo do projeto, devolvendo o aprendizado ao acervo e "
    "ao playbook. Não há ferramentas de execução.",
    [
        "Leia o relatório e o histórico das etapas: tentativas, feedbacks e decisões dos gates.",
        "Registre lições: o que funcionou, o que falhou e o que mudar no playbook.",
        (
            "Trate cada próximo passo do relatório: converta-o em uma nova ideia (título de até "
            "100 caracteres) ou descarte-o com motivo. Nenhum fica sem tratamento."
        ),
    ],
    {
        "licoes_aprendidas": (
            'JSON: lista (>= 1 item) de {"tipo": "funcionou"|"falhou"|"sugestao", '
            '"descricao": str, "evidencia": str (o episódio do projeto que a originou)}'
        ),
        "novas_ideias": (
            'JSON: lista (>= 1 item) de {"proximo_passo": str, "acao": "ideia"|"descartar", '
            '"motivo_descarte": str (obrigatório se descartar), "ideia": {"titulo": str (<= 100), '
            '"tipo": problema|oportunidade|hipotese|pergunta_de_pesquisa, "descricao": str, '
            '"pergunta_norteadora": str, "dominio": [str, ...] (>= 1), "valor": 1..5, '
            '"viabilidade": 1..5} (obrigatória se acao for ideia)}'
        ),
    },
    [
        "Rastreabilidade: toda lição aponta o episódio do projeto que a originou.",
        "Limitações declaradas: registre o que o projeto não conseguiu responder.",
    ],
)


def _etapa(
    codigo: str,
    ordem: int,
    nome: str,
    depende_de: tuple[str, ...],
    papel: str,
    ferramentas: tuple[str, ...],
    prompt: str,
    saidas: tuple[str, ...],
    criterio_saida: str,
    papel_revisor: str | None = None,
) -> Etapa:
    return Etapa(
        codigo=codigo,
        ordem=ordem,
        nome=nome,
        descricao=None,
        depende_de=depende_de,
        papel_agente=PapelAgente(papel, prompt, ferramentas),
        entradas=(),
        saidas=saidas,
        criterio_saida=criterio_saida,
        exige_gate=papel_revisor is not None,
        papel_revisor=papel_revisor,
    )


def _ler_json(ref: ArtefatoRef) -> tuple[object, str | None]:
    """Lê o artefato como JSON. Devolve `(dados, None)` ou `(None, motivo)`; nunca levanta.

    Só `file://` é suportado por enquanto.
    """
    partes = urlparse(ref.uri)
    if partes.scheme != "file":
        return None, f"esquema de URI não suportado ({ref.uri})"
    try:
        caminho = Path(url2pathname(unquote(partes.path)))
        return json.loads(caminho.read_text(encoding="utf-8")), None
    except OSError as erro:
        return None, f"arquivo ilegível ({erro.strerror or erro})"
    except ValueError as erro:  # JSON inválido ou texto que não é UTF-8
        return None, f"JSON inválido ({erro})"


def _preenchido(valor: object) -> bool:
    return bool(valor.strip()) if isinstance(valor, str) else bool(valor)


def _checar_catalogo(dados: object) -> list[str]:
    if not isinstance(dados, list):
        return ["catalogo_fontes deve ser uma lista JSON com um objeto por fonte."]
    if not dados:
        return ["catalogo_fontes está vazio: catalogue ao menos uma fonte."]
    feedback = []
    for i, item in enumerate(dados, start=1):
        if not isinstance(item, dict):
            feedback.append(f"catalogo_fontes: o item {i} não é um objeto JSON.")
            continue
        faltas = [c for c in ("licenca", "cobertura") if not _preenchido(item.get(c))]
        if faltas:
            nome = item.get("nome") or f"item {i}"
            feedback.append(f"catalogo_fontes: a fonte '{nome}' não informa {' e '.join(faltas)}.")
    return feedback


def _checar_cenarios(dados: object) -> list[str]:
    if not isinstance(dados, dict):
        return ["cenarios deve ser um objeto JSON com as chaves baixo, medio e alto."]
    ausentes = [c for c in CENARIOS_OBRIGATORIOS if c not in dados]
    feedback = []
    if ausentes:
        feedback.append(f"cenarios: faltam os cenários {', '.join(ausentes)}.")
    for chave in CENARIOS_OBRIGATORIOS:
        if chave in dados:
            cenario = dados[chave]
            if not isinstance(cenario, dict) or not _preenchido(cenario.get("intervalo")):
                feedback.append(f"cenarios: o cenário '{chave}' não informa o 'intervalo'.")
    return feedback


def _checar_novas_ideias(dados: object) -> list[str]:
    return ler_novas_ideias(dados)[1]


def _checar_licoes(dados: object) -> list[str]:
    return ler_licoes(dados)[1]


# etapa -> [(artefato JSON, verificação do conteúdo)]
_CHECAGENS_DE_CONTEUDO = {
    "coleta_dados": [("catalogo_fontes", _checar_catalogo)],
    "modelagem": [("cenarios", _checar_cenarios)],
    "retroalimentacao": [
        ("novas_ideias", _checar_novas_ideias),
        ("licoes_aprendidas", _checar_licoes),
    ],
}


@registrar
class ProjetoAcademicoSimples(Playbook):
    id = "projeto-academico-simples"
    versao = "0.1.0"
    nome = "Projeto Acadêmico Simples"
    descricao = (
        "Análise exploratória e descritiva com dados públicos, "
        "opcionalmente com projeções por cenário."
    )
    changelog = "Versão inicial (semente)."
    classe_de_problema = ["analise_exploratoria", "analise_descritiva", "projecao_por_cenarios"]
    pre_requisitos = [
        Requisito("pergunta_norteadora", "Pergunta respondível"),
        Requisito("dados", "Ao menos uma fonte pública acessível"),
        Requisito("ambiente", "Python com pandas, statsmodels, scikit-learn e geopandas"),
    ]
    template_projeto = {
        "secoes": [
            "definicao_problema",
            "escopo",
            "objetivos",
            "restricoes",
            "metodologia",
            "fontes_dados",
            "resultados_esperados",
            "riscos",
            "cronograma",
        ]
    }
    etapas = [
        _etapa(
            "contexto",
            1,
            "Revisão de contexto e literatura",
            (),
            "pesquisador",
            ("web_search", "web_fetch"),
            _PROMPT_CONTEXTO,
            ("nota_de_contexto",),
            "Nota de contexto com referências verificáveis",
        ),
        _etapa(
            "coleta_dados",
            2,
            "Inventário e coleta de dados",
            ("contexto",),
            "engenheiro_de_dados",
            ("web_fetch", "python"),
            _PROMPT_COLETA,
            ("catalogo_fontes", "dados_brutos"),
            "Fontes catalogadas com cobertura, licença e metadados",
            papel_revisor="responsavel_por_dados",
        ),
        _etapa(
            "harmonizacao",
            3,
            "Limpeza e harmonização",
            ("coleta_dados",),
            "engenheiro_de_dados",
            ("python",),
            _PROMPT_HARMONIZACAO,
            ("dataset_analitico", "dicionario_dados", "log_tratamento"),
            "Dataset analítico com dicionário e log",
        ),
        _etapa(
            "analise_exploratoria",
            4,
            "Análise exploratória",
            ("harmonizacao",),
            "analista",
            ("python",),
            _PROMPT_EXPLORATORIA,
            ("estatisticas_descritivas", "graficos"),
            "Estatísticas e gráficos cobrindo as variáveis do escopo",
        ),
        _etapa(
            "modelagem",
            5,
            "Modelagem e projeções",
            ("analise_exploratoria",),
            "modelador",
            ("python",),
            _PROMPT_MODELAGEM,
            ("modelos", "cenarios", "metricas_erro"),
            "Cenários com premissas, intervalos e métricas de erro",
            papel_revisor="revisor_metodologico",
        ),
        _etapa(
            "sintese",
            6,
            "Síntese e interpretação",
            ("modelagem",),
            "analista",
            (),
            _PROMPT_SINTESE,
            ("achados", "limitacoes"),
            "Achados ligados a evidências; limitações declaradas",
        ),
        _etapa(
            "relatorio",
            7,
            "Relatório e apresentação",
            ("sintese",),
            "redator",
            ("documentos", "slides"),
            _PROMPT_RELATORIO,
            ("relatorio", "apresentacao"),
            "Relatório e slides com insights, metodologia, resultados, discussão e próximos passos",
            papel_revisor="dono_do_projeto",
        ),
        _etapa(
            "retroalimentacao",
            8,
            "Retroalimentação",
            ("relatorio",),
            "curador",
            (),
            _PROMPT_RETROALIMENTACAO,
            ("licoes_aprendidas", "novas_ideias"),
            "Lições registradas e próximos passos convertidos ou descartados",
            papel_revisor="curador_do_acervo",
        ),
    ]
    criterios_qualidade = [
        Criterio("rastreabilidade", "Toda afirmação numérica rastreável a uma fonte"),
        Criterio(
            "observado_vs_projetado",
            "Dados observados e projetados sempre distinguidos visualmente",
        ),
        Criterio("incerteza", "Projeções com intervalo de incerteza e premissas explícitas"),
        Criterio("reprodutibilidade", "Código reexecutável do zero"),
        Criterio("limitacoes", "Limitações declaradas"),
    ]
    entregaveis = [
        Entregavel("relatorio"),
        Entregavel("apresentacao"),
        Entregavel("dataset"),
        Entregavel("codigo"),
    ]

    def aplicavel(self, ideia: IdeiaDTO) -> Avaliacao:
        """Aplicável sse há pergunta norteadora e ao menos uma fonte candidata.

        `pontuacao` = (pré-requisitos atendidos / 3) x fator do tipo, em [0, 1]. O requisito de
        ambiente conta como atendido (o sandbox o provê). O fator é 1.0 para os tipos
        `pergunta_de_pesquisa`, `problema` e `hipotese` e 0.5 para os demais; tipo
        incompatível não impede a aplicação, mas vira lacuna e reduz a pontuação.
        """
        lacunas = []
        atendidos = 1  # ambiente
        if ideia.pergunta_norteadora.strip():
            atendidos += 1
        else:
            lacunas.append("Falta a pergunta norteadora (pré-requisito: pergunta respondível).")
        if len(ideia.fontes_candidatas) >= 1:
            atendidos += 1
        else:
            lacunas.append("Nenhuma fonte candidata (pré-requisito: ao menos uma fonte pública).")
        fator = 1.0
        if ideia.tipo not in TIPOS_COMPATIVEIS:
            fator = FATOR_TIPO_INCOMPATIVEL
            lacunas.append(
                f"O tipo '{ideia.tipo}' não é o típico deste playbook "
                f"({', '.join(sorted(TIPOS_COMPATIVEIS))})."
            )
        return Avaliacao(
            aplicavel=atendidos == TOTAL_PRE_REQUISITOS,
            pontuacao=atendidos / TOTAL_PRE_REQUISITOS * fator,
            lacunas=lacunas,
        )

    def instanciar(self, ideia: IdeiaDTO) -> EspecProjeto:
        """Esqueleto determinístico; objetivos, escopo, riscos etc. ficam para o redator."""
        por_codigo = {e.codigo: e for e in self.etapas}
        marcos = []
        for codigo, etapa_codigo in MARCOS:
            etapa = por_codigo[etapa_codigo]
            responsavel = (
                f"humano:{etapa.papel_revisor}"
                if etapa.exige_gate
                else f"agente:{etapa.papel_agente.papel}"
            )
            marcos.append(
                MarcoPlanejado(
                    codigo=codigo,
                    nome=etapa.nome,
                    etapa_codigo=etapa.codigo,
                    criterio_saida=etapa.criterio_saida,
                    exige_gate=etapa.exige_gate,
                    papel_revisor=etapa.papel_revisor,
                    responsavel=responsavel,
                )
            )
        return EspecProjeto(
            definicao_problema=DefinicaoProblema(
                contexto=ideia.descricao, pergunta_norteadora=ideia.pergunta_norteadora
            ),
            metodologia=Metodologia(playbook_codigo=self.id, playbook_versao=self.versao),
            fontes_dados=[
                Fonte(nome=f["nome"], url=f.get("url"), status_acesso="nao_testado")
                for f in ideia.fontes_candidatas
            ],
            cronograma=marcos,
        )

    def validar_etapa(self, etapa: Etapa, saida: SaidaEtapa) -> Resultado:
        """Checagens mecânicas; os critérios semânticos ficam com o revisor LLM."""
        feedback = []
        faltantes = [nome for nome in etapa.saidas if nome not in saida.artefatos]
        if faltantes:
            feedback.append(f"Faltam artefatos obrigatórios: {', '.join(faltantes)}.")
        for nome, verificar in _CHECAGENS_DE_CONTEUDO.get(etapa.codigo, []):
            if nome in saida.artefatos:
                dados, erro = _ler_json(saida.artefatos[nome])
                feedback += [f"Não foi possível ler {nome}: {erro}."] if erro else verificar(dados)
        return Resultado(aprovado=not feedback, feedback=feedback)
