"""execucao: chamada LLM pode pertencer a uma ideia (enriquecimento 1.1) (12)

Revision ID: 0010
Revises: 0009
"""

from ais4.migracao_util import executar_sql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    executar_sql("12_chamada_llm_ideia.sql")


def downgrade() -> None:
    raise NotImplementedError("o schema execucao não tem downgrade")
