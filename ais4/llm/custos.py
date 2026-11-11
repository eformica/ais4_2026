"""Preços por modelo e cálculo de custo em USD (sempre Decimal, 6 casas)."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

MTOK = Decimal(1_000_000)
CASAS = Decimal("0.000001")  # numeric(12,6) no banco


@dataclass(frozen=True)
class Preco:
    """USD por milhão de tokens. `cache_leitura=None`: preço desconhecido."""

    entrada: Decimal
    saida: Decimal
    cache_leitura: Decimal | None


PRECOS: dict[str, Preco] = {
    "claude-opus-5-5": Preco(Decimal("4.00"), Decimal("20.00"), Decimal("0.20")),
    "claude-sonnet-5-5": Preco(Decimal("2.00"), Decimal("10.00"), Decimal("0.20")),
    "claude-haiku-5-5": Preco(Decimal("0.10"), Decimal("0.50"), None),
    # Modelos de fallback do servidor (`fallbacks="default"`): quem responde pode ser um deles.
    # Preço de cache desconhecido: o cliente trata leitura/escrita de cache neles como imprecisa.
    "claude-opus-4-8": Preco(Decimal("5.00"), Decimal("25.00"), None),
    "claude-opus-5": Preco(Decimal("5.00"), Decimal("25.00"), None),
    "claude-sonnet-5": Preco(Decimal("2.00"), Decimal("10.00"), None),
}

# PROVISÓRIO: USD por busca web de servidor (Config.preco_busca_web sobrepõe este padrão).
PRECO_BUSCA_WEB = Decimal("0.01")


def calcular_custo(
    modelo: str,
    tokens_entrada: int,
    tokens_saida: int,
    tokens_cache_leitura: int = 0,
    buscas_web: int = 0,
    *,
    preco_busca_web: Decimal | None = None,
) -> Decimal:
    """Custo em USD com 6 casas decimais.

    `tokens_entrada` exclui os tokens lidos do cache (como em `usage.input_tokens`).
    Levanta `KeyError` para modelo sem preço, e `ValueError` se houve leitura de cache
    em um modelo cujo preço de cache é desconhecido: nunca custa zero em silêncio.
    """
    preco = PRECOS[modelo]
    if tokens_cache_leitura > 0 and preco.cache_leitura is None:
        raise ValueError(f"Preço de leitura de cache desconhecido para {modelo}")
    por_busca = PRECO_BUSCA_WEB if preco_busca_web is None else preco_busca_web
    total = (
        Decimal(tokens_entrada) * preco.entrada
        + Decimal(tokens_saida) * preco.saida
        + Decimal(tokens_cache_leitura) * (preco.cache_leitura or Decimal(0))
    ) / MTOK + Decimal(buscas_web) * por_busca
    return total.quantize(CASAS, rounding=ROUND_HALF_UP)
