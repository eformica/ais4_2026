"""outbox de todos os módulos: tentativas, último erro e estacionamento (13)

Revision ID: 0011
Revises: 0010
"""

from ais4.migracao_util import executar_sql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    executar_sql("13_outbox_estacionamento.sql")


def downgrade() -> None:
    raise NotImplementedError("os outboxes não têm downgrade")
