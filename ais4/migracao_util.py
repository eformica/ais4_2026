from pathlib import Path

from alembic import op

DIRETORIO_SQL = Path(__file__).resolve().parent.parent / "db" / "sql"


def executar_sql(nome_arquivo: str) -> None:
    """Executa um arquivo de db/sql inteiro pelo cursor psycopg cru, sem parâmetros.

    Sem parâmetros, o psycopg usa o protocolo simples: `%` (RAISE) e `::` (casts)
    não são interpretados e vários comandos cabem numa só chamada.
    """
    texto = (DIRETORIO_SQL / nome_arquivo).read_text(encoding="utf-8")
    cursor = op.get_bind().connection.driver_connection.cursor()
    try:
        cursor.execute(texto)
    finally:
        cursor.close()
