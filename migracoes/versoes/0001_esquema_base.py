"""esquema base: papéis, playbooks, acervo e projetos (00-03)

Revision ID: 0001
Revises:
"""

from ais4.migracao_util import executar_sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ARQUIVOS = ["00_roles_schemas.sql", "01_playbooks.sql", "02_acervo.sql", "03_projetos.sql"]


def upgrade() -> None:
    for arquivo in ARQUIVOS:
        executar_sql(arquivo)


def downgrade() -> None:
    raise NotImplementedError("o esquema base não tem downgrade")
