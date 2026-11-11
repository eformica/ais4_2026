"""Porta `ClienteLLM` e sua implementação sobre o SDK `anthropic` (2.x).

Todo uso de LLM no sistema passa por aqui: modelo e effort por papel (Config), custo por
chamada, e falhas tipadas (`RespostaInvalida`, `OrcamentoExcedido`).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from ais4.config import Config, obter_config
from ais4.llm.custos import CASAS, MTOK, PRECOS, calcular_custo

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

Motivo = Literal["refusal", "max_tokens", "schema", "sem_conclusao"]

BETA_FALLBACK = "server-side-fallback-2026-07-01"
MAX_TOKENS_ESTRUTURADO = 16000
MAX_TOKENS_AGENTE = 64000
EFFORT_PADRAO = "high"

# Ferramentas de servidor: executam nos servidores da Anthropic, sem função local.
FERRAMENTA_BUSCA_WEB: dict[str, str] = {"type": "web_search_20260209", "name": "web_search"}
FERRAMENTA_LEITURA_WEB: dict[str, str] = {"type": "web_fetch_20260209", "name": "web_fetch"}


@dataclass(frozen=True)
class Uso:
    """Consumo de uma resposta do modelo (uma por mensagem, não por chamada de `agente`)."""

    modelo: str
    tokens_entrada: int  # exclui os tokens lidos do cache
    tokens_saida: int
    tokens_cache_leitura: int
    buscas_web: int
    custo_usd: Decimal
    duracao_ms: int
    request_id: str | None


class RespostaInvalida(Exception):
    """A resposta não serve: conta como falha da tentativa, com o motivo registrável."""

    def __init__(self, motivo: Motivo, detalhe: str = "") -> None:
        super().__init__(f"{motivo}: {detalhe}" if detalhe else motivo)
        self.motivo: Motivo = motivo
        self.detalhe = detalhe


class OrcamentoExcedido(Exception):
    """Levantada por `ao_usar` para interromper o agente antes da próxima chamada.

    `dimensao` diz qual limite estourou (`tokens`, `custo` ou `prazo`); `consumido` e `limite`
    são os valores comparados (int, Decimal ou datetime, conforme a dimensão). Todos opcionais:
    quem só tem uma mensagem continua podendo usar `OrcamentoExcedido("texto")`.
    """

    def __init__(
        self,
        mensagem: str = "",
        *,
        dimensao: Literal["tokens", "custo", "prazo"] | None = None,
        consumido: Any = None,
        limite: Any = None,
    ) -> None:
        if not mensagem:
            mensagem = "orçamento excedido"
            if dimensao is not None:
                mensagem = (
                    f"orçamento de {dimensao} excedido: consumido {consumido}, limite {limite}"
                )
        super().__init__(mensagem)
        self.dimensao = dimensao
        self.consumido = consumido
        self.limite = limite


class ClienteLLM(Protocol):
    def estruturado(
        self,
        *,
        papel: str,
        sistema: str,
        mensagem: str,
        formato: type[T],
        ao_usar: Callable[[Uso], None],
    ) -> T:
        """Uma chamada cuja resposta é validada contra o modelo Pydantic `formato`."""
        ...

    def agente(
        self,
        *,
        papel: str,
        sistema: str,
        mensagem: str,
        ferramentas: list,
        ao_usar: Callable[[Uso], None],
        max_iteracoes: int = 40,
    ) -> str:
        """Laço de ferramentas; devolve o texto final. `ao_usar` roda a cada mensagem do
        modelo, antes das ferramentas dela e da próxima chamada, e pode levantar
        `OrcamentoExcedido` para interromper."""
        ...


def _detalhe_recusa(mensagem: Any) -> str:
    detalhes = getattr(mensagem, "stop_details", None)
    if detalhes is None:
        return "o modelo recusou a solicitação"
    partes = [p for p in (detalhes.category, detalhes.explanation) if p]
    return ": ".join(partes) if partes else "o modelo recusou a solicitação"


def _verificar_parada(mensagem: Any) -> None:
    """Checa `stop_reason` ANTES de ler o conteúdo (recusa/corte não trazem resultado útil)."""
    motivo = mensagem.stop_reason
    if motivo == "refusal":
        raise RespostaInvalida("refusal", _detalhe_recusa(mensagem))
    if motivo in ("max_tokens", "model_context_window_exceeded"):
        raise RespostaInvalida("max_tokens", f"stop_reason={motivo}")


def _texto(mensagem: Any) -> str:
    return "".join(b.text for b in mensagem.content if b.type == "text")


def _custo_parcela(
    modelo: str, entrada: int, saida: int, cache_leitura: int, cache_escrita: int
) -> Decimal:
    """Custo de uma tentativa, preço do próprio `modelo`, sem nunca levantar nem zerar.

    Cobrança conservadora, com `logger.warning`, quando falta preço: modelo fora de PRECOS,
    leitura de cache sem preço de cache, ou escrita de cache (sem preço cadastrado:
    entrada x 1,25). Conservadora = a maior tarifa por token de PRECOS (modelo desconhecido)
    ou a tarifa de entrada do modelo (cache).
    """
    maior_entrada = max(p.entrada for p in PRECOS.values())
    maior_saida = max(p.saida for p in PRECOS.values())
    preco = PRECOS.get(modelo)
    if preco is None:
        logger.warning("Modelo sem preço cadastrado: %s; cobrando a tarifa mais cara", modelo)
        base = (entrada + cache_leitura + cache_escrita * Decimal("1.25")) * maior_entrada
        return (base + saida * maior_saida) / MTOK
    custo = calcular_custo(modelo, entrada, saida, 0, 0, preco_busca_web=Decimal(0))
    if cache_leitura:
        if preco.cache_leitura is None:
            logger.warning("Leitura de cache sem preço para %s; cobrando como entrada", modelo)
            custo += cache_leitura * preco.entrada / MTOK
        else:
            custo += cache_leitura * preco.cache_leitura / MTOK
    if cache_escrita:
        logger.warning(
            "Escrita de cache sem preço cadastrado (%s); cobrando entrada x 1,25", modelo
        )
        custo += cache_escrita * preco.entrada * Decimal("1.25") / MTOK
    return custo


class ClienteAnthropic(ClienteLLM):
    def __init__(self, client: anthropic.Anthropic | None = None, config: Config | None = None):
        self._client = client
        self._config = config

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = obter_config()
        return self._config

    @property
    def sdk(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def _modelo_e_effort(self, papel: str) -> tuple[str, str]:
        config = self.config
        modelo = config.modelo_por_papel.get(papel, config.modelo_padrao)
        return modelo, config.effort_por_papel.get(papel, EFFORT_PADRAO)

    def _uso(self, mensagem: Any, pedido: str, inicio: float, request_id: str | None) -> Uso:
        """Uso da mensagem. Nunca levanta por preço desconhecido: o gasto já aconteceu e
        precisa chegar ao `ao_usar` (veja `_custo_parcela`)."""
        usage = mensagem.usage
        # Com fallback do servidor, quem responde pode ser outro modelo.
        final = mensagem.model or pedido
        # `usage.iterations` é a fonte por tentativa: uma tentativa recusada antes do
        # fallback também é cobrada e só aparece ali (o `usage` de topo cobre só a que
        # produziu a mensagem devolvida).
        parcelas = [
            (
                getattr(it, "model", None) or final,
                it.input_tokens,
                it.output_tokens,
                it.cache_read_input_tokens,
                it.cache_creation_input_tokens,
            )
            for it in (usage.iterations or [])
        ] or [
            (
                final,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_input_tokens or 0,
                usage.cache_creation_input_tokens or 0,
            )
        ]
        buscas = usage.server_tool_use.web_search_requests if usage.server_tool_use else 0
        custo = sum((_custo_parcela(*p) for p in parcelas), Decimal(0))
        custo += Decimal(buscas) * self.config.preco_busca_web
        return Uso(
            final,
            sum(p[1] for p in parcelas),
            sum(p[2] for p in parcelas),
            sum(p[3] for p in parcelas),
            buscas,
            custo.quantize(CASAS, rounding=ROUND_HALF_UP),
            int((time.perf_counter() - inicio) * 1000),
            request_id,
        )

    def estruturado(
        self,
        *,
        papel: str,
        sistema: str,
        mensagem: str,
        formato: type[T],
        ao_usar: Callable[[Uso], None],
    ) -> T:
        modelo, effort = self._modelo_e_effort(papel)
        # `beta.messages.create` com o schema do SDK, e não `.parse()`: o `.parse()` valida o
        # JSON dentro do próprio SDK, antes de devolver a mensagem, e então uma recusa com
        # texto ou um JSON cortado por max_tokens chegariam como ValidationError, sem
        # `stop_reason` para classificar o motivo.
        inicio = time.perf_counter()
        resposta = self.sdk.beta.messages.create(
            model=modelo,
            max_tokens=MAX_TOKENS_ESTRUTURADO,
            system=sistema,
            messages=[{"role": "user", "content": mensagem}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": anthropic.transform_schema(formato)},
            },
            thinking={"type": "adaptive"},
            betas=[BETA_FALLBACK],
            fallbacks="default",
        )
        ao_usar(self._uso(resposta, modelo, inicio, getattr(resposta, "_request_id", None)))
        _verificar_parada(resposta)
        texto = _texto(resposta)
        if not texto.strip():
            raise RespostaInvalida("sem_conclusao", "a resposta não trouxe texto")
        try:
            return formato.model_validate_json(texto)
        except ValidationError as erro:
            raise RespostaInvalida("schema", str(erro)) from erro

    def agente(
        self,
        *,
        papel: str,
        sistema: str,
        mensagem: str,
        ferramentas: list,
        ao_usar: Callable[[Uso], None],
        max_iteracoes: int = 40,
    ) -> str:
        modelo, effort = self._modelo_e_effort(papel)
        # stream=True: com max_tokens=64000 o SDK recusa requisições não-streaming (tempo
        # esperado > 10 min). thread=False: o histórico completo vai a cada turno, sem
        # estado de conversa mantido no servidor.
        runner = self.sdk.beta.messages.tool_runner(
            model=modelo,
            max_tokens=MAX_TOKENS_AGENTE,
            system=sistema,
            messages=[{"role": "user", "content": mensagem}],
            tools=ferramentas,
            max_iterations=max_iteracoes,
            output_config={"effort": effort},
            thinking={"type": "adaptive"},
            betas=[BETA_FALLBACK],
            fallbacks="default",
            thread=False,
            stream=True,
            # Explícito: o `ao_usar` antes das ferramentas depende de elas só rodarem depois
            # do corpo do laço (não durante o streaming); o padrão do SDK vai mudar.
            run_tools_eagerly=False,
        )
        ultima = None
        # O runner executa as ferramentas dentro do `next()` seguinte, só depois que o corpo
        # do laço devolve o controle: `ao_usar` levantar aqui impede essa execução e a
        # próxima requisição. Por isso a duração de cada mensagem após a primeira inclui o
        # tempo das ferramentas da anterior.
        inicio = time.perf_counter()
        try:
            for fluxo in runner:
                resposta = fluxo.get_final_message()
                ao_usar(self._uso(resposta, modelo, inicio, getattr(fluxo, "request_id", None)))
                ultima = resposta
                inicio = time.perf_counter()
        finally:
            runner.close()
        if ultima is None:
            raise RespostaInvalida("sem_conclusao", "o modelo não respondeu")
        _verificar_parada(ultima)
        if ultima.stop_reason not in ("end_turn", "stop_sequence"):
            raise RespostaInvalida(
                "sem_conclusao", f"o laço terminou com stop_reason={ultima.stop_reason}"
            )
        texto = _texto(ultima)
        if not texto.strip():
            raise RespostaInvalida("sem_conclusao", "a mensagem final não trouxe texto")
        return texto
