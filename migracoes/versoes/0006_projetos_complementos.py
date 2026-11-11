"""responsáveis do projeto e trilha de transições do marco (08)

Revision ID: 0006
Revises: 0005
"""

from ais4.migracao_util import executar_sql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    executar_sql("08_projetos_complementos.sql")


def downgrade() -> None:
    raise NotImplementedError("os complementos de projetos não têm downgrade")
