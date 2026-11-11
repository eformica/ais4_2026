from decimal import Decimal

import pytest

from ais4.llm.custos import PRECO_BUSCA_WEB, PRECOS, calcular_custo


def test_custo_opus():
    assert calcular_custo("claude-opus-5-5", 1_000_000, 100_000, 0, 0) == Decimal("6.00")


def test_custo_sonnet_com_cache():
    # 2.00 + 10.00 * 0.5 + 0.20 * 3 = 7.60
    custo = calcular_custo("claude-sonnet-5-5", 1_000_000, 500_000, 3_000_000, 0)
    assert custo == Decimal("7.60")


def test_custo_haiku_sem_cache():
    assert calcular_custo("claude-haiku-5-5", 1_000_000, 2_000_000, 0, 0) == Decimal("1.10")


def test_haiku_nao_tem_preco_de_cache_e_cache_lido_levanta():
    assert PRECOS["claude-haiku-5-5"].cache_leitura is None
    with pytest.raises(ValueError, match="cache"):
        calcular_custo("claude-haiku-5-5", 10, 10, 1, 0)


def test_busca_web_soma_preco_por_busca():
    assert PRECO_BUSCA_WEB == Decimal("0.01")
    assert calcular_custo("claude-opus-5-5", 0, 0, 0, 3) == Decimal("0.03")
    custo = calcular_custo("claude-opus-5-5", 0, 0, 0, 3, preco_busca_web=Decimal("0.02"))
    assert custo == Decimal("0.06")


def test_custo_tem_seis_casas_decimais():
    custo = calcular_custo("claude-opus-5-5", 1, 1, 0, 0)  # 0.000004 + 0.000020
    assert custo == Decimal("0.000024")
    assert custo.as_tuple().exponent == -6


def test_modelo_desconhecido_levanta():
    with pytest.raises(KeyError):
        calcular_custo("modelo-que-nao-existe", 1, 1, 0, 0)
