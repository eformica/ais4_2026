"""schema execucao: orçamento, tarefas por tentativa e chamadas LLM (10)

Revision ID: 0008
Revises: 0007
"""

from ais4.migracao_util import executar_sql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    executar_sql("10_execucao.sql")


def downgrade() -> None:
    raise NotImplementedError("o schema execucao não tem downgrade")
