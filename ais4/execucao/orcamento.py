"""Orçamento do projeto (tokens, custo, prazo) e registro das chamadas ao LLM.

Consumo de tokens = `tokens_entrada + tokens_saida + tokens_cache_leitura` (a view `consumo`).
O orçamento estoura quando o consumo ATINGE o limite (>=) ou quando `agora > prazo`; `prazo` NULL
significa sem prazo. Chamadas sem `projeto_id` (Motor de Criação, que tem `proposta_id`, e
enriquecimento do Acervo, que tem `ideia_id`) são registradas mas não entram em nenhum orçamento
de projeto.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ais4.comum.dto import ConsumoDTO, OrcamentoDTO
from ais4.comum.erros import RegraViolada
from ais4.config import obter_config
from ais4.db import sessao
from ais4.execucao.modelos import ChamadaLLMModelo, ConsumoModelo, OrcamentoModelo
from ais4.execucao.servico import USUARIO_SISTEMA
from ais4.llm.cliente import OrcamentoExcedido, Uso

_CENTAVOS = Decimal("0.01")


def _dto(linha: OrcamentoModelo) -> OrcamentoDTO:
    return OrcamentoDTO(
        projeto_id=linha.projeto_id,
        max_tokens=linha.max_tokens,
        max_custo_usd=linha.max_custo_usd,
        prazo=linha.prazo,
    )


def definir(
    projeto_id: UUID,
    max_tokens: int,
    max_custo_usd: Decimal,
    prazo: datetime | None,
    usuario: str,
) -> None:
    """Cria ou substitui o orçamento do projeto. `prazo` sem fuso é lido como UTC."""
    if max_tokens <= 0 or max_custo_usd <= 0:
        raise RegraViolada("O orçamento precisa de max_tokens e max_custo_usd positivos")
    if prazo is not None and prazo.tzinfo is None:
        prazo = prazo.replace(tzinfo=UTC)
    valores = {
        "max_tokens": max_tokens,
        "max_custo_usd": Decimal(max_custo_usd).quantize(_CENTAVOS),
        "prazo": prazo,
    }
    with sessao("execucao", usuario) as s:
        s.execute(
            pg_insert(OrcamentoModelo)
            .values(projeto_id=projeto_id, **valores)
            .on_conflict_do_update(index_elements=[OrcamentoModelo.projeto_id], set_=valores)
        )


def obter_ou_padrao(projeto_id: UUID, usuario: str) -> OrcamentoDTO:
    """O orçamento do projeto; no primeiro uso grava o padrão da config (sem prazo) e o devolve."""
    cfg = obter_config()
    padrao = {
        "max_tokens": cfg.orcamento_padrao_tokens,
        "max_custo_usd": Decimal(str(cfg.orcamento_padrao_usd)).quantize(_CENTAVOS),
        "prazo": None,
    }
    with sessao("execucao", usuario) as s:
        s.execute(
            pg_insert(OrcamentoModelo)
            .values(projeto_id=projeto_id, **padrao)
            .on_conflict_do_nothing(index_elements=[OrcamentoModelo.projeto_id])
        )
        return _dto(
            s.execute(
                select(OrcamentoModelo).where(OrcamentoModelo.projeto_id == projeto_id)
            ).scalar_one()
        )


def consumo(projeto_id: UUID) -> ConsumoDTO:
    """Tokens e custo acumulados do projeto (zeros se ainda não houve chamada)."""
    with sessao("execucao", USUARIO_SISTEMA) as s:
        linha = s.get(ConsumoModelo, projeto_id)
    if linha is None:
        return ConsumoDTO(projeto_id, 0, Decimal(0))
    return ConsumoDTO(projeto_id, linha.tokens, linha.custo_usd)


def custo_da_proposta(proposta_id: UUID) -> Decimal:
    """Custo (USD) somado das chamadas registradas com `proposta_id` (zero se não houve)."""
    with sessao("execucao", USUARIO_SISTEMA) as s:
        total = s.scalar(
            select(func.coalesce(func.sum(ChamadaLLMModelo.custo_usd), 0)).where(
                ChamadaLLMModelo.proposta_id == proposta_id
            )
        )
    return Decimal(total)


def registrar_chamada(
    uso: Uso,
    projeto_id: UUID | None,
    tarefa_id: UUID | None,
    proposta_id: UUID | None,
    ideia_id: UUID | None = None,
) -> None:
    """Grava a chamada em `execucao.chamada_llm`. Sempre grava: o gasto já aconteceu.

    A chamada precisa de um dono: projeto, proposta ou ideia (`ideia_id` é referência lógica a
    `acervo.ideia`, sem FK; R30). Sem nenhum, o banco recusa (`RegraViolada`).
    """
    with sessao("execucao", USUARIO_SISTEMA) as s:
        s.add(
            ChamadaLLMModelo(
                tarefa_id=tarefa_id,
                proposta_id=proposta_id,
                projeto_id=projeto_id,
                ideia_id=ideia_id,
                modelo=uso.modelo,
                tokens_entrada=uso.tokens_entrada,
                tokens_saida=uso.tokens_saida,
                tokens_cache_leitura=uso.tokens_cache_leitura,
                buscas_web=uso.buscas_web,
                custo_usd=uso.custo_usd,
                duracao_ms=uso.duracao_ms,
                request_id=uso.request_id,
            )
        )


def verificar(projeto_id: UUID) -> None:
    """Levanta `OrcamentoExcedido` se o consumo atingiu um limite ou o prazo venceu.

    Sem orçamento definido vale o padrão da config (que passa a ser gravado). A ordem de checagem
    é tokens, custo, prazo; só a primeira dimensão estourada é reportada.
    """
    orcamento = obter_ou_padrao(projeto_id, USUARIO_SISTEMA)
    gasto = consumo(projeto_id)
    if gasto.tokens >= orcamento.max_tokens:
        raise OrcamentoExcedido(
            dimensao="tokens", consumido=gasto.tokens, limite=orcamento.max_tokens
        )
    if gasto.custo_usd >= orcamento.max_custo_usd:
        raise OrcamentoExcedido(
            dimensao="custo", consumido=gasto.custo_usd, limite=orcamento.max_custo_usd
        )
    agora = datetime.now(UTC)
    if orcamento.prazo is not None and agora > orcamento.prazo:
        raise OrcamentoExcedido(dimensao="prazo", consumido=agora, limite=orcamento.prazo)


def vigiar(projeto_id: UUID, tarefa_id: UUID | None) -> Callable[[Uso], None]:
    """Callback para `ao_usar`: registra a chamada e, só depois, verifica o orçamento."""

    def ao_usar(uso: Uso) -> None:
        registrar_chamada(uso, projeto_id, tarefa_id, None)
        verificar(projeto_id)

    return ao_usar
