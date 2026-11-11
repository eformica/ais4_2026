import json
import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from testcontainers.community.postgres import PostgresContainer

from ais4 import config, db

RAIZ = Path(__file__).resolve().parent.parent
IMAGEM = "pgvector/pgvector:0.8.0-pg17"
SCHEMAS_MODULO = ("playbooks", "acervo", "projetos", "criacao", "execucao")


def _limpar_caches() -> None:
    db.engine.cache_clear()
    config.obter_config.cache_clear()


@pytest.fixture(scope="session")
def _ambiente_config():
    """Variáveis AIS4_* ativas durante a sessão; restauradas ao final."""
    anteriores: dict[str, str | None] = {}

    def definir(valores: dict[str, str]) -> None:
        for chave, valor in valores.items():
            anteriores.setdefault(chave, os.environ.get(chave))
            os.environ[chave] = valor
        _limpar_caches()

    yield definir
    for chave, valor in anteriores.items():
        if valor is None:
            os.environ.pop(chave, None)
        else:
            os.environ[chave] = valor
    db.engine.cache_clear()
    config.obter_config.cache_clear()


@pytest.fixture(scope="session")
def banco(_ambiente_config):
    """URL admin (psycopg puro) de um container pgvector já migrado até head.

    Antes de migrar, aponta a Config para o container e define as senhas dos
    papéis `*_app` (a migração 0003 as lê da Config).
    """
    with PostgresContainer(IMAGEM, username="postgres", password="postgres", dbname="ais4") as pg:
        url_psycopg = (
            f"postgresql://postgres:postgres@{pg.get_container_host_ip()}"
            f":{pg.get_exposed_port(5432)}/ais4"
        )
        _ambiente_config(
            {
                "AIS4_DB_HOST": pg.get_container_host_ip(),
                "AIS4_DB_PORTA": str(pg.get_exposed_port(5432)),
                "AIS4_DB_NOME": "ais4",
                "AIS4_SENHA_APP": json.dumps({m: f"senha-{m}" for m in SCHEMAS_MODULO}),
            }
        )
        cfg = AlembicConfig(str(RAIZ / "alembic.ini"))
        cfg.set_main_option("script_location", str(RAIZ / "migracoes"))
        cfg.set_main_option("version_locations", str(RAIZ / "migracoes" / "versoes"))
        cfg.attributes["db_url"] = url_psycopg.replace("postgresql://", "postgresql+psycopg://", 1)
        command.upgrade(cfg, "head")
        yield url_psycopg


@pytest.fixture
def limpar(banco):
    """Esvazia as tabelas dos schemas de módulo e recarrega a semente."""
    with psycopg.connect(banco, autocommit=True) as c:
        tabelas = c.execute(
            "select format('%%I.%%I', table_schema, table_name) from information_schema.tables "
            "where table_type = 'BASE TABLE' and table_schema = any(%s)",
            [list(SCHEMAS_MODULO)],
        ).fetchall()
        if tabelas:
            c.execute("TRUNCATE " + ", ".join(t[0] for t in tabelas) + " RESTART IDENTITY CASCADE")
        c.execute((RAIZ / "db" / "sql" / "04_seed.sql").read_text(encoding="utf-8"))
    yield
