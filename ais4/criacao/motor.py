"""Motor de Criação, passos 1 a 3 do fluxo 4.2: seleção, casamento com playbook e fontes.

Sem transação entre módulos: a reserva da ideia é uma `proposta` ativa em `criacao`, e o índice
único parcial `proposta_ideia_ativa_uq` garante que dois motores concorrentes não reservem a mesma
ideia (quem perde a disputa recebe a próxima da fila).
"""

import json
import logging
from collections.abc import Callable
from typing import Literal
from uuid import UUID

import anthropic
from anthropic import beta_tool
from pydantic import ValidationError
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ais4.acervo import servico as acervo
from ais4.auth import Usuario
from ais4.comum.dto import IdeiaDTO
from ais4.comum.erros import NaoEncontrado, RegraViolada
from ais4.config import obter_config
from ais4.criacao import agentes
from ais4.criacao.modelos import FonteVerificada, FonteVerificadaModelo, PropostaModelo
from ais4.db import sessao
from ais4.llm.cliente import (
    FERRAMENTA_BUSCA_WEB,
    FERRAMENTA_LEITURA_WEB,
    ClienteLLM,
    RespostaInvalida,
    Uso,
)
from ais4.playbooks import registro
from ais4.playbooks import servico as playbooks
from ais4.playbooks.base import Avaliacao, Playbook
from ais4.projetos import servico as projetos

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


def registrar_uso_noop(uso: Uso) -> None:
    """Padrão de `ao_usar` até a Task 13 ligar o registro em `execucao.chamada_llm`."""


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
    ao_usar: Callable[[Uso], None] = registrar_uso_noop,
) -> list[FonteVerificada]:
    """Verifica cada fonte candidata da ideia (um agente por fonte) e grava o resultado.

    Falha de uma fonte (resposta inválida, erro da API, agente sem registro) vira `inacessivel`
    com a explicação em `evidencia`; fonte sem URL que o agente não localiza vira
    `nao_localizada`. Cada fonte é gravada assim que verificada. `OrcamentoExcedido` propaga.
    """
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
