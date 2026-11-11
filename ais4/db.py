from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Literal

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ais4.comum import erros
from ais4.config import obter_config

Modulo = Literal["playbooks", "acervo", "projetos", "criacao", "execucao"]


@lru_cache
def engine(modulo: Modulo) -> Engine:
    """Engine do módulo, logado como `<modulo>_app` (só enxerga o próprio schema)."""
    cfg = obter_config()
    url = URL.create(
        "postgresql+psycopg",
        username=f"{modulo}_app",
        password=cfg.senha_app[modulo],
        host=cfg.db_host,
        port=cfg.db_porta,
        database=cfg.db_nome,
    )
    return create_engine(url, pool_pre_ping=True)


@contextmanager
def sessao(modulo: Modulo, usuario: str) -> Iterator[Session]:
    """Transação do módulo com o autor registrado em `app.usuario`.

    Commit ao sair; rollback em qualquer exceção. Erros do banco são traduzidos.
    """
    s = Session(engine(modulo), expire_on_commit=False)
    try:
        s.execute(text("select set_config('app.usuario', :u, true)"), {"u": usuario})
        yield s
        s.commit()
    except DBAPIError as exc:
        s.rollback()
        traduzido = erros.traduzir(exc)
        if traduzido is exc:
            raise
        raise traduzido from exc
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()
