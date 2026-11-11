"""schema criacao: propostas, iterações de autocrítica e fontes verificadas (09)

Revision ID: 0007
Revises: 0006
"""

from ais4.migracao_util import executar_sql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    executar_sql("09_criacao.sql")


def downgrade() -> None:
    raise NotImplementedError("o schema criacao não tem downgrade")
