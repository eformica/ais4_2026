"""semente: playbook v0.1.0 e ideia piloto (04)

Revision ID: 0002
Revises: 0001
"""

from ais4.migracao_util import executar_sql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    executar_sql("04_seed.sql")


def downgrade() -> None:
    raise NotImplementedError("a semente não tem downgrade")
