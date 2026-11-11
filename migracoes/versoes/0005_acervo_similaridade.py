"""similaridade do acervo: pgvector, lacunas e revisão de duplicidade (07)

Revision ID: 0005
Revises: 0004
"""

from ais4.migracao_util import executar_sql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    executar_sql("07_acervo_similaridade.sql")


def downgrade() -> None:
    raise NotImplementedError("a similaridade do acervo não tem downgrade")
