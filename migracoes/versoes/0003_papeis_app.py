"""papéis de aplicação por módulo (05) e senhas a partir da config

Revision ID: 0003
Revises: 0002
"""

from alembic import op
from psycopg import sql

from ais4.config import obter_config
from ais4.migracao_util import executar_sql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

MODULOS = ("playbooks", "acervo", "projetos", "criacao", "execucao")


def upgrade() -> None:
    executar_sql("05_papeis_app.sql")
    senhas = obter_config().senha_app
    faltam = [m for m in MODULOS if m not in senhas]
    if faltam:
        raise RuntimeError(f"AIS4_SENHA_APP sem senha para: {', '.join(faltam)}")
    cursor = op.get_bind().connection.driver_connection.cursor()
    try:
        for modulo in MODULOS:
            comando = sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(f"{modulo}_app"), sql.Literal(senhas[modulo])
            )
            cursor.execute(comando)
    finally:
        cursor.close()


def downgrade() -> None:
    raise NotImplementedError("os papéis de aplicação não têm downgrade")
