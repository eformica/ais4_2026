"""complementos de playbooks: proposto_por e proposta_melhoria (06)

Revision ID: 0004
Revises: 0003
"""

from ais4.migracao_util import executar_sql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    executar_sql("06_playbooks_complementos.sql")


def downgrade() -> None:
    raise NotImplementedError("os complementos de playbooks não têm downgrade")
