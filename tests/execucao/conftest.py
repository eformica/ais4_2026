from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest

from ais4.execucao import orcamento
from ais4.llm.cliente import Uso
from tests.projetos.fabrica import novo_projeto


@pytest.fixture
def projeto_id(banco, limpar) -> UUID:
    return novo_projeto().id


def uso(
    custo="0.10",
    entrada=100,
    saida=50,
    cache=0,
    buscas=0,
    request_id="req_1",
) -> Uso:
    return Uso(
        modelo="claude-opus-5-5",
        tokens_entrada=entrada,
        tokens_saida=saida,
        tokens_cache_leitura=cache,
        buscas_web=buscas,
        custo_usd=Decimal(custo),
        duracao_ms=12,
        request_id=request_id,
    )


def definir(projeto_id, tokens=1_000_000, usd="10.00", prazo=None):
    orcamento.definir(projeto_id, tokens, Decimal(usd), prazo, "ana")


def amanha() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


def ontem() -> datetime:
    return datetime.now(UTC) - timedelta(days=1)


def consultar(banco, sql, params=()):
    with psycopg.connect(banco) as c:
        return c.execute(sql, params).fetchall()
