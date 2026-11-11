from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from testcontainers.community.postgres import PostgresContainer

RAIZ = Path(__file__).resolve().parent.parent
IMAGEM = "pgvector/pgvector:0.8.0-pg17"
SCHEMAS_MODULO = ("playbooks", "acervo", "projetos", "criacao", "execucao")


@pytest.fixture(scope="session")
def banco():
    """URL admin (psycopg puro) de um container pgvector já migrado até head."""
    with PostgresContainer(IMAGEM, username="postgres", password="postgres", dbname="ais4") as pg:
        url_psycopg = (
            f"postgresql://postgres:postgres@{pg.get_container_host_ip()}"
            f":{pg.get_exposed_port(5432)}/ais4"
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
