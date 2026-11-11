"""Motor de Criação, fluxo 4.2: seleção, playbook, fontes, rascunho, autocrítica e gate humano.

Sem transação entre módulos: a reserva da ideia é uma `proposta` ativa em `criacao`, e o índice
único parcial `proposta_ideia_ativa_uq` garante que dois motores concorrentes não reservem a mesma
ideia (quem perde a disputa recebe a próxima da fila).
"""

import json
import logging
import threading
from collections.abc import Callable
from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import anthropic
from anthropic import beta_tool
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ais4.acervo import servico as acervo
from ais4.auth import Usuario
from ais4.comum.dto import IdeiaDTO, IteracaoDTO, PropostaDTO
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.comum.espec import EspecProjeto, Fonte
from ais4.config import obter_config
from ais4.criacao import agentes
from ais4.criacao.modelos import (
    FonteVerificada,
    FonteVerificadaModelo,
    IteracaoModelo,
    PropostaModelo,
)
from ais4.criacao.validacao import Lacuna, ResultadoCritica, validar_espec
from ais4.db import sessao
from ais4.execucao import orcamento
from ais4.llm.cliente import (
    FERRAMENTA_BUSCA_WEB,
    FERRAMENTA_LEITURA_WEB,
    ClienteLLM,
    RespostaInvalida,
    Uso,
)
from ais4.playbooks import registro
from ais4.playbooks import servico as playbooks
from ais4.playbooks.base import Avaliacao, Etapa, Playbook
from ais4.projetos import servico as projetos
from ais4.projetos.servico import CriarProjeto

log = logging.getLogger(__name__)

USUARIO_SISTEMA = "sistema:motor-criacao"  # autor das escritas sem usuário humano
LIMITE_FILA = 100  # quantas ideias qualificadas a seleção examina por chamada
INDICE_IDEIA_ATIVA = "proposta_ideia_ativa_uq"
ERROS_SISTEMICOS = (
    anthropic.BadRequestError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
)
MAX_ITERACOES_VERIFICACAO = 20  # mensagens do modelo por fonte (busca, leitura, registro)
PAPEL_DONO = "dono_do_projeto"
# o projeto de uma proposta tem id determinístico: repetir a aprovação não duplica o projeto
NAMESPACE_PROJETOS = uuid5(NAMESPACE_URL, "urn:ais4:criacao:projeto")
STATUS_EM_ANDAMENTO = ("rascunho", "em_autocritica", "ajustes_solicitados")
SECAO_SISTEMA = "sistema"  # lacunas que não vêm de um critério de aceite
SECAO_REVISAO_HUMANA = "revisao_humana"


def registrar_uso_noop(uso: Uso) -> None:
    """`ao_usar` que não faz nada (testes). O padrão do motor grava em `execucao.chamada_llm`."""


def _registrar_na_proposta(proposta_id: UUID) -> Callable[[Uso], None]:
    """Padrão de `ao_usar`: grava cada chamada em `execucao.chamada_llm` com `proposta_id`.

    Sem `projeto_id`: o Motor de Criação ainda não tem projeto, então não há orçamento a vigiar.
    """

    def registrar(uso: Uso) -> None:
        orcamento.registrar_chamada(uso, projeto_id=None, tarefa_id=None, proposta_id=proposta_id)

    return registrar


class DecisaoProposta(BaseModel):
    """O gate de aprovação humana (passo 6): aprovar, pedir ajustes ou rejeitar com motivo."""

    acao: Literal["aprovar", "ajustar", "rejeitar"]
    motivo: str | None = None
    ajustes: list[str] = []
    responsaveis: dict[str, str] = {}  # papel -> pessoa; o dono é a pessoa que aprova


# ---------------------------------------------------------------------------
# 1. Seleção e reserva
# ---------------------------------------------------------------------------
def _perdeu_a_disputa_pela_ideia(erro: RegraViolada) -> bool:
    """Violação do índice único parcial (e não de outra regra), vista pela causa do erro."""
    causa = getattr(getattr(erro.__cause__, "orig", None), "diag", None)
    return getattr(causa, "constraint_name", None) == INDICE_IDEIA_ATIVA


def selecionar_e_reservar(usuario: str) -> tuple[IdeiaDTO, UUID] | None:
    """Reserva a ideia `qualificada` de maior prioridade que ainda não tem proposta ativa.

    Devolve `(ideia, proposta_id)` com a proposta em `rascunho`, ou `None` se não há ideia livre
    ou se o número de projetos `em_execucao` já atingiu `AIS4_MAX_PROJETOS_EM_EXECUCAO`.
    """
    if projetos.contar_por_status("em_execucao") >= obter_config().max_projetos_em_execucao:
        return None
    for ideia in acervo.fila("qualificada", LIMITE_FILA):
        try:
            with sessao("criacao", usuario) as s:
                proposta = PropostaModelo(ideia_id=ideia.id, solicitado_por=usuario)
                s.add(proposta)
                s.flush()
                proposta_id = proposta.id
        except RegraViolada as erro:
            if not _perdeu_a_disputa_pela_ideia(erro):
                raise
            log.info("ideia %s já reservada por outra proposta; tentando a próxima", ideia.id)
            continue
        return ideia, proposta_id
    return None


# ---------------------------------------------------------------------------
# 2. Casamento com o playbook
# ---------------------------------------------------------------------------
def casar_playbook(ideia: IdeiaDTO) -> tuple[Playbook, Avaliacao] | None:
    """Entre as versões ativas, a aplicável de maior pontuação (empate: `validado`).

    Versões ativas no banco sem classe registrada no código são ignoradas (com aviso).
    """
    candidatos: list[tuple[float, bool, Playbook, Avaliacao]] = []
    for versao in playbooks.versoes_ativas():
        try:
            playbook = registro.obter(versao.codigo, versao.versao)
        except NaoEncontrado:
            log.warning("versão ativa %s %s sem classe registrada", versao.codigo, versao.versao)
            continue
        avaliacao = playbook.aplicavel(ideia)
        if avaliacao.aplicavel:
            candidatos.append(
                (avaliacao.pontuacao, versao.status == "validado", playbook, avaliacao)
            )
    if not candidatos:
        return None
    _, _, playbook, avaliacao = max(candidatos, key=lambda c: (c[0], c[1]))
    return playbook, avaliacao


def vincular_playbook(proposta_id: UUID, playbook: Playbook, usuario: str) -> None:
    """Grava na proposta o playbook e a versão que `casar_playbook` escolheu."""
    versao = playbooks.obter_versao(playbook.id, playbook.versao)
    with sessao("criacao", usuario) as s:
        s.execute(
            update(PropostaModelo)
            .where(PropostaModelo.id == proposta_id)
            .values(playbook_id=versao.playbook_id, playbook_versao=versao.versao)
        )


def devolver_sem_playbook(
    proposta_id: UUID,
    ideia: IdeiaDTO,
    lacunas: list[str],
    usuario: str,
    classes: list[str] | None = None,
) -> None:
    """Sem playbook compatível: proposta `devolvida`, lacuna registrada e ideia de volta à triagem.

    `classes` são as classes de problema que faltam; por padrão ficam vazias (desconhecidas) e o
    Acervo reabre a lacuna quando qualquer playbook for validado (o curador reavalia).
    """
    descricao = "; ".join(lacunas) or "Nenhum playbook ativo é aplicável à ideia."
    with sessao("criacao", usuario) as s:
        s.execute(
            update(PropostaModelo)
            .where(PropostaModelo.id == proposta_id)
            .values(status="devolvida", motivo=descricao)
        )
    # O motor age em nome do Acervo ao registrar a lacuna: assume o papel de curador só aqui.
    curador = Usuario(usuario, frozenset({acervo.PAPEL_CURADOR}))
    acervo.registrar_lacuna(ideia.id, descricao, list(classes or []), curador)
    acervo.transicionar(ideia.id, "triagem", usuario, f"Sem playbook aplicável: {descricao}")


# ---------------------------------------------------------------------------
# 3. Verificação das fontes
# ---------------------------------------------------------------------------
def _mensagem_verificacao(ideia: IdeiaDTO, nome: str, url: str | None) -> str:
    url_informada = url if url else "nenhuma (sem URL: localize a URL oficial)"
    return (
        f"Fonte candidata: {nome}\n"
        f"URL informada: {url_informada}\n\n"
        f"Ideia que usaria esta fonte: {ideia.titulo}\n"
        f"Pergunta norteadora: {ideia.pergunta_norteadora}\n"
        f"Domínio: {', '.join(ideia.dominio)}\n"
        f"Recorte e restrições: {ideia.restricoes or 'não informado'}"
    )


def _erros_legiveis(erro: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'registro'}: {e['msg']}" for e in erro.errors()
    )


def _verificar_uma_fonte(
    cliente: ClienteLLM,
    ideia: IdeiaDTO,
    nome_fonte: str,
    url_fonte: str | None,
    ao_usar: Callable[[Uso], None],
) -> FonteVerificada:
    registradas: list[FonteVerificada] = []

    @beta_tool(strict=True)
    def registrar_verificacao(
        nome: str,
        url: str | None,
        status: Literal["acessivel", "inacessivel", "nao_localizada"],
        cobertura: str,
        evidencia: str,
    ) -> str:
        """Registra o resultado da verificação da fonte. Chame exatamente uma vez, ao terminar.

        Args:
            nome: Nome da fonte, como foi pedido.
            url: URL oficial testada, ou null se não foi localizada.
            status: "acessivel", "inacessivel" ou "nao_localizada".
            cobertura: Objeto JSON, em texto, com temporal, geografica, variaveis, licenca e
                periodicidade observadas (ou texto livre).
            evidencia: Texto curto e verificável que sustenta o status e a cobertura.
        """
        if registradas:
            return "Erro: a verificação desta fonte já foi registrada; encerre a resposta."
        try:
            lida = json.loads(cobertura)
        except ValueError:
            lida = None
        cobertura = lida if isinstance(lida, dict) else {"descricao": cobertura}
        try:
            # O nome gravado é sempre o da fonte candidata, mesmo que o modelo o reescreva.
            registradas.append(
                FonteVerificada(
                    nome=nome_fonte,
                    url=url,
                    status=status,
                    cobertura=cobertura,
                    evidencia=evidencia,
                )
            )
        except ValidationError as erro:
            return f"Erro: registro inválido ({_erros_legiveis(erro)}). Corrija e chame de novo."
        return "Verificação registrada."

    motivo_falha: str | None = None
    try:
        cliente.agente(
            papel=agentes.PAPEL_VERIFICADOR,
            sistema=agentes.PROMPT_VERIFICADOR_FONTES,
            mensagem=_mensagem_verificacao(ideia, nome_fonte, url_fonte),
            ferramentas=[FERRAMENTA_BUSCA_WEB, FERRAMENTA_LEITURA_WEB, registrar_verificacao],
            ao_usar=ao_usar,
            max_iteracoes=MAX_ITERACOES_VERIFICACAO,
        )
    except ERROS_SISTEMICOS:
        raise  # configuração ou schema errados afetariam todas as fontes: não é falha da fonte
    except (RespostaInvalida, anthropic.APIError) as erro:
        # OrcamentoExcedido não é capturada: pausar e alertar é decisão de quem chamou.
        motivo_falha = f"A verificação falhou ({type(erro).__name__}: {erro})."
        log.warning("verificação da fonte %r falhou: %s", nome_fonte, motivo_falha)

    if registradas:  # um registro válido feito antes da falha continua valendo
        return registradas[0]
    return FonteVerificada(
        nome=nome_fonte,
        url=url_fonte,
        status="inacessivel",
        evidencia=motivo_falha
        or "O agente terminou sem chamar registrar_verificacao; acesso não confirmado.",
    )


def _gravar_fonte(proposta_id: UUID, fonte: FonteVerificada) -> None:
    valores = {
        "url": fonte.url,
        "status": fonte.status,
        "cobertura": fonte.cobertura,
        "evidencia": fonte.evidencia,
    }
    with sessao("criacao", USUARIO_SISTEMA) as s:
        s.execute(
            pg_insert(FonteVerificadaModelo.__table__)
            .values(proposta_id=proposta_id, nome=fonte.nome, **valores)
            .on_conflict_do_update(
                index_elements=["proposta_id", "nome"],
                set_={**valores, "testado_em": func.now()},
            )
        )


def verificar_fontes(
    proposta_id: UUID,
    ideia: IdeiaDTO,
    cliente: ClienteLLM,
    ao_usar: Callable[[Uso], None] | None = None,
) -> list[FonteVerificada]:
    """Verifica cada fonte candidata da ideia (um agente por fonte) e grava o resultado.

    Falha de uma fonte (resposta inválida, erro da API, agente sem registro) vira `inacessivel`
    com a explicação em `evidencia`; fonte sem URL que o agente não localiza vira
    `nao_localizada`. Cada fonte é gravada assim que verificada. `OrcamentoExcedido` propaga.
    `ao_usar` padrão: grava cada chamada em `execucao.chamada_llm` com `proposta_id`.
    """
    if ao_usar is None:
        ao_usar = _registrar_na_proposta(proposta_id)
    resultado: list[FonteVerificada] = []
    vistas: set[str] = set()
    for candidata in ideia.fontes_candidatas:
        nome = candidata.get("nome") or candidata.get("url")
        if not nome or nome in vistas:
            continue
        vistas.add(nome)
        fonte = _verificar_uma_fonte(cliente, ideia, nome, candidata.get("url"), ao_usar)
        _gravar_fonte(proposta_id, fonte)
        resultado.append(fonte)
    return resultado


# ---------------------------------------------------------------------------
# Leitura das propostas
# ---------------------------------------------------------------------------
def _dto_proposta(p: PropostaModelo) -> PropostaDTO:
    return PropostaDTO(
        id=p.id,
        ideia_id=p.ideia_id,
        playbook_id=p.playbook_id,
        playbook_versao=p.playbook_versao,
        status=p.status,
        conteudo=p.conteudo,
        iteracoes=p.iteracoes,
        motivo=p.motivo,
        solicitado_por=p.solicitado_por,
        decidido_por=p.decidido_por,
        decidido_em=p.decidido_em,
        projeto_id=p.projeto_id,
        criado_em=p.criado_em,
        atualizado_em=p.atualizado_em,
    )


def _dto_iteracao(i: IteracaoModelo) -> IteracaoDTO:
    return IteracaoDTO(
        proposta_id=i.proposta_id,
        n=i.n,
        rascunho=i.rascunho,
        lacunas=list(i.lacunas),
        aprovada_pela_critica=i.aprovada_pela_critica,
        custo_usd=i.custo_usd,
        criado_em=i.criado_em,
    )


def listar_propostas(status: str | None = None, limite: int = 50) -> list[PropostaDTO]:
    """Propostas da mais recente para a mais antiga, opcionalmente de um só status."""
    consulta = select(PropostaModelo)
    if status is not None:
        consulta = consulta.where(PropostaModelo.status == status)
    with sessao("criacao", USUARIO_SISTEMA) as s:
        linhas = s.scalars(consulta.order_by(PropostaModelo.criado_em.desc()).limit(limite))
        return [_dto_proposta(p) for p in linhas]


def obter_proposta(proposta_id: UUID) -> PropostaDTO:
    with sessao("criacao", USUARIO_SISTEMA) as s:
        linha = s.get(PropostaModelo, proposta_id)
        if linha is None:
            raise NaoEncontrado(f"Proposta {proposta_id} não encontrada")
        return _dto_proposta(linha)


def iteracoes_da_proposta(proposta_id: UUID) -> list[IteracaoDTO]:
    with sessao("criacao", USUARIO_SISTEMA) as s:
        linhas = s.scalars(
            select(IteracaoModelo)
            .where(IteracaoModelo.proposta_id == proposta_id)
            .order_by(IteracaoModelo.n)
        )
        return [_dto_iteracao(i) for i in linhas]


def fontes_da_proposta(proposta_id: UUID) -> list[FonteVerificada]:
    """As fontes que `verificar_fontes` gravou para a proposta."""
    with sessao("criacao", USUARIO_SISTEMA) as s:
        linhas = s.scalars(
            select(FonteVerificadaModelo)
            .where(FonteVerificadaModelo.proposta_id == proposta_id)
            .order_by(FonteVerificadaModelo.testado_em, FonteVerificadaModelo.nome)
        )
        return [
            FonteVerificada(
                nome=f.nome,
                url=f.url,
                status=f.status,
                cobertura=dict(f.cobertura),
                evidencia=f.evidencia,
            )
            for f in linhas
        ]


# ---------------------------------------------------------------------------
# 4. Rascunho
# ---------------------------------------------------------------------------
def _json(valor: object) -> str:
    return json.dumps(valor, ensure_ascii=False, indent=2, default=str)


def _texto(valor: object) -> str:
    return ", ".join(map(str, valor)) if isinstance(valor, list) else str(valor)


def _fonte_da_espec(fonte: FonteVerificada) -> Fonte:
    """Converte o resultado da verificação (cobertura em dicionário) no `Fonte` da especificação."""
    cobertura = dict(fonte.cobertura)
    periodicidade = _texto(cobertura.pop("periodicidade", ""))
    licenca = _texto(cobertura.pop("licenca", ""))
    descricao = cobertura.pop("descricao", None)
    partes = (
        [_texto(descricao)] if descricao else [f"{k}: {_texto(v)}" for k, v in cobertura.items()]
    )
    return Fonte(
        nome=fonte.nome,
        url=fonte.url,
        cobertura="; ".join(partes),
        periodicidade=periodicidade,
        licenca=licenca,
        status_acesso=fonte.status,
    )


def _playbook_da_proposta(proposta: PropostaDTO) -> Playbook:
    for versao in playbooks.listar_versoes():
        if (versao.playbook_id, versao.versao) == (proposta.playbook_id, proposta.playbook_versao):
            return registro.obter(versao.codigo, versao.versao)
    raise NaoEncontrado(f"Playbook da proposta {proposta.id} não encontrado")


def _mensagem_redacao(
    ideia: IdeiaDTO,
    esqueleto: EspecProjeto,
    fontes: list[FonteVerificada],
    anterior: dict | None,
    feedback: list[str],
) -> str:
    blocos = [
        "Ideia qualificada:\n"
        + _json(
            {
                "titulo": ideia.titulo,
                "tipo": ideia.tipo,
                "descricao": ideia.descricao,
                "pergunta_norteadora": ideia.pergunta_norteadora,
                "dominio": ideia.dominio,
                "restricoes": ideia.restricoes,
            }
        ),
        "Esqueleto do playbook (preencha e complete; metodologia e fontes_dados serão "
        "sobrescritas pelo sistema):\n" + _json(esqueleto.model_dump(mode="json")),
        "Fontes verificadas (únicas permitidas em fontes_dados):\n"
        + _json([f.model_dump(mode="json") for f in fontes]),
    ]
    if anterior:
        blocos.append("Rascunho anterior (corrija-o, não recomece):\n" + _json(anterior))
    if feedback:
        blocos.append("Lacunas a corrigir:\n" + "\n".join(f"- {item}" for item in feedback))
    return "\n\n".join(blocos)


def redigir(
    proposta_id: UUID,
    cliente: ClienteLLM,
    feedback: list[str],
    ao_usar: Callable[[Uso], None] | None = None,
) -> EspecProjeto:
    """O `redator_projeto` preenche a `EspecProjeto` a partir do esqueleto do playbook.

    A metodologia (playbook e versão) e as fontes de dados vêm do sistema, não do LLM: o que o
    redator escreveu nessas seções é descartado. `RespostaInvalida` propaga. `ao_usar` padrão:
    grava a chamada em `execucao.chamada_llm` com `proposta_id`.
    """
    if ao_usar is None:
        ao_usar = _registrar_na_proposta(proposta_id)
    proposta = obter_proposta(proposta_id)
    ideia = acervo.obter(proposta.ideia_id)
    playbook = _playbook_da_proposta(proposta)
    fontes = fontes_da_proposta(proposta_id)
    esboco = cliente.estruturado(
        papel=agentes.PAPEL_REDATOR,
        sistema=agentes.PROMPT_REDATOR_PROJETO,
        mensagem=_mensagem_redacao(
            ideia, playbook.instanciar(ideia), fontes, proposta.conteudo, feedback
        ),
        formato=EspecProjeto,
        ao_usar=ao_usar,
    )
    metodologia = esboco.metodologia.model_copy(
        update={"playbook_codigo": playbook.id, "playbook_versao": playbook.versao}
    )
    return esboco.model_copy(
        update={
            "metodologia": metodologia,
            "fontes_dados": [_fonte_da_espec(f) for f in fontes],
        }
    )


# ---------------------------------------------------------------------------
# 5. Autocrítica
# ---------------------------------------------------------------------------
def autocriticar(
    proposta_id: UUID,
    espec: EspecProjeto,
    etapas: list[Etapa],
    cliente: ClienteLLM,
    ao_usar: Callable[[Uso], None] | None = None,
) -> list[Lacuna]:
    """Lacunas do validador determinístico + as do `critico_projeto` (critérios semânticos).

    Se o crítico não responde de forma válida, isso vira uma lacuna (a rodada não é aprovada sem
    crítica) e as lacunas determinísticas seguem valendo. `ao_usar` padrão: grava a chamada em
    `execucao.chamada_llm` com `proposta_id`.
    """
    if ao_usar is None:
        ao_usar = _registrar_na_proposta(proposta_id)
    lacunas = validar_espec(espec, etapas)
    mensagem = "Rascunho da especificação:\n" + _json(espec.model_dump(mode="json"))
    if lacunas:
        achadas = "\n".join(f"- {x.secao}: {x.mensagem}" for x in lacunas)
        mensagem += (
            f"\n\nLacunas já detectadas por validação automática (não as repita):\n{achadas}"
        )
    try:
        critica = cliente.estruturado(
            papel=agentes.PAPEL_CRITICO,
            sistema=agentes.PROMPT_CRITICO_PROJETO,
            mensagem=mensagem,
            formato=ResultadoCritica,
            ao_usar=ao_usar,
        )
    except RespostaInvalida as erro:
        log.warning("crítico indisponível na proposta %s: %s", proposta_id, erro.motivo)
        indisponivel = Lacuna(secao=SECAO_SISTEMA, mensagem=f"crítica indisponível: {erro.motivo}")
        return [*lacunas, indisponivel]
    return [*lacunas, *critica.lacunas]


def _formatar(lacuna: Lacuna | dict) -> str:
    if isinstance(lacuna, dict):
        lacuna = Lacuna.model_validate(lacuna)
    return f"{lacuna.secao}: {lacuna.mensagem}"


def _atualizar_proposta(proposta_id: UUID, usuario: str, **valores: object) -> None:
    with sessao("criacao", usuario) as s:
        s.execute(update(PropostaModelo).where(PropostaModelo.id == proposta_id).values(**valores))


def _proximo_n(s: Session, proposta_id: UUID) -> int:
    atual = s.scalar(
        select(func.max(IteracaoModelo.n)).where(IteracaoModelo.proposta_id == proposta_id)
    )
    return (atual or 0) + 1


def _gravar_iteracao(
    proposta_id: UUID,
    usuario: str,
    novo_rascunho: dict | None,
    lacunas: list[Lacuna],
    custo: Decimal,
) -> None:
    """Uma rodada: a linha em `proposta_iteracao` e o estado corrente da proposta, juntos.

    Rodada sem rascunho novo (a redação falhou) repete o último rascunho, ou `{}` se não há.
    """
    with sessao("criacao", usuario) as s:
        proposta = s.get(PropostaModelo, proposta_id)
        n = _proximo_n(s, proposta_id)
        rascunho = novo_rascunho if novo_rascunho is not None else proposta.conteudo
        s.add(
            IteracaoModelo(
                proposta_id=proposta_id,
                n=n,
                rascunho=rascunho or {},
                lacunas=[x.model_dump() for x in lacunas],
                aprovada_pela_critica=novo_rascunho is not None and not lacunas,
                custo_usd=custo,
            )
        )
        proposta.iteracoes = n
        if novo_rascunho is not None:
            proposta.conteudo = novo_rascunho


def _executar_rodadas(
    proposta_id: UUID,
    cliente: ClienteLLM,
    feedback: list[str],
    ao_usar: Callable[[Uso], None],
    usuario: str,
) -> None:
    """Redige e critica até não haver lacunas ou esgotar `AIS4_MAX_ITERACOES_AUTOCRITICA` rodadas.

    Termina em `aguardando_aprovacao` (com as lacunas restantes na última iteração) ou, se
    nunca houve rascunho válido, em `devolvida`.
    """
    proposta = obter_proposta(proposta_id)
    etapas = playbooks.etapas(proposta.playbook_id, proposta.playbook_versao)
    tem_rascunho = proposta.conteudo is not None
    _atualizar_proposta(proposta_id, usuario, status="em_autocritica")

    for _ in range(obter_config().max_iteracoes_autocritica):
        custo = Decimal(0)

        def contar(uso: Uso) -> None:
            nonlocal custo
            custo += uso.custo_usd
            ao_usar(uso)

        espec: EspecProjeto | None = None
        try:
            espec = redigir(proposta_id, cliente, feedback, contar)
        except RespostaInvalida as erro:
            lacunas = [Lacuna(secao=SECAO_SISTEMA, mensagem=f"redação falhou: {erro.motivo}")]
        else:
            lacunas = autocriticar(proposta_id, espec, etapas, cliente, contar)
            feedback = [_formatar(x) for x in lacunas]

        novo = espec.model_dump(mode="json") if espec is not None else None
        _gravar_iteracao(proposta_id, usuario, novo, lacunas, custo)
        tem_rascunho = tem_rascunho or novo is not None
        if espec is not None and not lacunas:
            break

    if tem_rascunho:
        _atualizar_proposta(proposta_id, usuario, status="aguardando_aprovacao")
    else:
        _atualizar_proposta(proposta_id, usuario, status="devolvida", motivo="falha na redação")


def _retomar_ajustada(usuario: str) -> tuple[UUID, list[str]] | None:
    """Reivindica, de forma atômica, a proposta `ajustes_solicitados` mais antiga.

    Devolve o id e o feedback (as lacunas da última iteração, isto é, os ajustes do revisor).
    """
    alvo = (
        select(PropostaModelo.id)
        .where(PropostaModelo.status == "ajustes_solicitados")
        .order_by(PropostaModelo.atualizado_em, PropostaModelo.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    with sessao("criacao", usuario) as s:
        linha = s.execute(
            update(PropostaModelo)
            .where(PropostaModelo.id == alvo)
            .values(status="em_autocritica")
            .returning(PropostaModelo.id)
        ).first()
        if linha is None:
            return None
        ultima = s.scalars(
            select(IteracaoModelo)
            .where(IteracaoModelo.proposta_id == linha.id)
            .order_by(IteracaoModelo.n.desc())
            .limit(1)
        ).first()
        return linha.id, [_formatar(x) for x in (ultima.lacunas if ultima else [])]


def _devolver_por_erro(proposta_id: UUID, erro: BaseException, usuario: str) -> None:
    """Nunca deixa uma proposta ativa travando a ideia: o erro original é o que importa."""
    try:
        with sessao("criacao", usuario) as s:
            s.execute(
                update(PropostaModelo)
                .where(PropostaModelo.id == proposta_id)
                .where(PropostaModelo.status.in_(STATUS_EM_ANDAMENTO))
                .values(status="devolvida", motivo=f"{type(erro).__name__}: {erro}"[:1000])
            )
    except Exception:
        log.exception("não foi possível devolver a proposta %s após o erro", proposta_id)


def executar_ciclo(
    cliente: ClienteLLM,
    usuario_sistema: str = "motor-criacao",
    ao_usar: Callable[[Uso], None] | None = None,
) -> UUID | None:
    """Passos 1 a 5 do fluxo 4.2 para uma proposta; devolve o id dela, ou `None` sem trabalho.

    Retoma primeiro a proposta `ajustes_solicitados` mais antiga (sem reverificar as fontes);
    senão reserva a ideia `qualificada` de maior prioridade. Qualquer exceção depois da reserva
    devolve a proposta (a ideia segue `qualificada`) e é relançada, inclusive em interrupção
    (Ctrl+C/SIGTERM). `ao_usar` padrão: grava cada chamada em `execucao.chamada_llm` com o
    `proposta_id` (sem `projeto_id`); um `ao_usar` informado o substitui.
    """
    retomada = _retomar_ajustada(usuario_sistema)
    if retomada is not None:
        proposta_id, feedback = retomada
        ideia = None
    else:
        reserva = selecionar_e_reservar(usuario_sistema)
        if reserva is None:
            return None
        ideia, proposta_id = reserva
        feedback = []
    if ao_usar is None:
        ao_usar = _registrar_na_proposta(proposta_id)
    try:
        if ideia is not None:
            casado = casar_playbook(ideia)
            if casado is None:
                devolver_sem_playbook(proposta_id, ideia, [], usuario_sistema)
                return proposta_id
            vincular_playbook(proposta_id, casado[0], usuario_sistema)
            verificar_fontes(proposta_id, ideia, cliente, ao_usar)
        _executar_rodadas(proposta_id, cliente, feedback, ao_usar, usuario_sistema)
    except BaseException as erro:  # inclui KeyboardInterrupt/SystemExit: nunca deixa proposta ativa
        _devolver_por_erro(proposta_id, erro, usuario_sistema)
        raise
    return proposta_id


def loop(
    cliente: ClienteLLM,
    intervalo_s: float = 30.0,
    parar: threading.Event | None = None,
    usuario_sistema: str = USUARIO_SISTEMA,
) -> None:
    """Um ciclo a cada `intervalo_s` até `parar`. Erro de um ciclo é registrado e não mata o laço.

    O intervalo vale também depois de um ciclo com trabalho: uma ideia cuja redação falha volta
    à fila, e sem a pausa ela seria reprocessada sem parar, gastando a API.
    """
    parar = parar or threading.Event()
    while not parar.is_set():
        try:
            executar_ciclo(cliente, usuario_sistema)
        except Exception:
            log.exception("ciclo do motor de criação falhou")
        parar.wait(intervalo_s)


# ---------------------------------------------------------------------------
# 6 e 7. Gate de aprovação humana e registro do projeto
# ---------------------------------------------------------------------------
def _aprovar(linha: PropostaModelo, decisao: DecisaoProposta, usuario: Usuario) -> None:
    ideia = acervo.obter(linha.ideia_id)
    try:
        comando = CriarProjeto(
            titulo=ideia.titulo,
            ideia_origem_id=linha.ideia_id,
            playbook_id=linha.playbook_id,
            playbook_versao=linha.playbook_versao,
            dono=usuario.nome,
            especificacao=EspecProjeto.model_validate(linha.conteudo),
            aprovado_por=usuario.nome,
            responsaveis=decisao.responsaveis,
            id=uuid5(NAMESPACE_PROJETOS, str(linha.id)),
        )
    except ValidationError as erro:
        raise RegraViolada(_erros_legiveis(erro)) from erro
    linha.projeto_id = projetos.criar_projeto(comando, usuario.nome).id
    linha.status = "aprovada"
    # O outbox só entregaria ProjetoCriado depois; até lá a ideia seria selecionada de novo.
    # O handler do acervo ignora a transição se a ideia já está em_projeto.
    if acervo.obter(linha.ideia_id).status == "qualificada":
        acervo.transicionar(
            linha.ideia_id, "em_projeto", usuario.nome, f"projeto {linha.projeto_id} criado"
        )


def _ajustar(s: Session, linha: PropostaModelo, decisao: DecisaoProposta) -> None:
    ajustes = [a.strip() for a in decisao.ajustes if a.strip()]
    if not ajustes:
        raise RegraViolada("Ajustar exige ao menos um item em 'ajustes'")
    n = _proximo_n(s, linha.id)
    # Sem coluna própria para o revisor: o ajuste humano é uma iteração com lacunas da revisão.
    s.add(
        IteracaoModelo(
            proposta_id=linha.id,
            n=n,
            rascunho=linha.conteudo,
            lacunas=[Lacuna(secao=SECAO_REVISAO_HUMANA, mensagem=a).model_dump() for a in ajustes],
            aprovada_pela_critica=False,
            custo_usd=Decimal(0),
        )
    )
    linha.iteracoes = n
    linha.status = "ajustes_solicitados"


def decidir(proposta_id: UUID, decisao: DecisaoProposta, usuario: Usuario) -> PropostaDTO:
    """Gate de aprovação: só o dono do projeto, e nunca quem solicitou a proposta.

    A linha fica bloqueada durante a decisão: duas decisões simultâneas não criam dois projetos.
    """
    if PAPEL_DONO not in usuario.papeis:
        raise Proibido(f"Decidir uma proposta exige o papel {PAPEL_DONO!r}")
    with sessao("criacao", usuario.nome) as s:
        linha = s.scalars(
            select(PropostaModelo).where(PropostaModelo.id == proposta_id).with_for_update()
        ).first()
        if linha is None:
            raise NaoEncontrado(f"Proposta {proposta_id} não encontrada")
        if usuario.nome == linha.solicitado_por:
            raise Proibido("Quem solicitou a proposta não pode decidi-la")
        if linha.status != "aguardando_aprovacao":
            raise RegraViolada(
                f"A proposta está '{linha.status}'; só 'aguardando_aprovacao' aceita decisão"
            )
        motivo = (decisao.motivo or "").strip()
        if decisao.acao == "rejeitar" and not motivo:
            raise RegraViolada("Rejeitar exige um motivo")

        if decisao.acao == "aprovar":
            _aprovar(linha, decisao, usuario)
        elif decisao.acao == "ajustar":
            _ajustar(s, linha, decisao)
        else:
            linha.status = "rejeitada"
            acervo.transicionar(
                linha.ideia_id, "triagem", usuario.nome, f"Proposta rejeitada: {motivo}"
            )
        linha.motivo = motivo or None
        linha.decidido_por = usuario.nome
        linha.decidido_em = func.now()
        s.flush()
        s.refresh(linha)
        return _dto_proposta(linha)
