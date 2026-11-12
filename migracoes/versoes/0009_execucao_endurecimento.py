"""execucao: uma tentativa ativa por etapa, tarefa terminal imutável, dono da chamada (11)

Revision ID: 0009
Revises: 0008
"""

from ais4.migracao_util import executar_sql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    executar_sql("11_execucao_endurecimento.sql")


def downgrade() -> None:
    raise NotImplementedError("o schema execucao não tem downgrade")
