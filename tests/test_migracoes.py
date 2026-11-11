import importlib.util
from pathlib import Path

import psycopg
import pytest

RAIZ = Path(__file__).resolve().parent.parent


def test_semente_presente(banco):
    with psycopg.connect(banco) as c:
        assert (
            c.execute(
                "select status from playbooks.playbook_versao where versao='0.1.0'"
            ).fetchone()[0]
            == "experimental"
        )
        assert c.execute("select count(*) from playbooks.etapa").fetchone()[0] == 8
        assert (
            c.execute(
                "select status from acervo.ideia where id='00000000-0000-4000-8000-000000000101'"
            ).fetchone()[0]
            == "triagem"
        )


def test_versao_publicada_imutavel(banco):
    with psycopg.connect(banco) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("update playbooks.playbook_versao set changelog='x' where versao='0.1.0'")


def test_downgrade_nao_suportado():
    caminho = RAIZ / "migracoes" / "versoes" / "0001_esquema_base.py"
    spec = importlib.util.spec_from_file_location("revisao_0001", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    with pytest.raises(NotImplementedError):
        modulo.downgrade()


def test_limpar_restaura_semente(banco, limpar):
    with psycopg.connect(banco) as c:
        assert c.execute("select count(*) from playbooks.etapa").fetchone()[0] == 8
        assert c.execute("select count(*) from acervo.ideia").fetchone()[0] == 1
        assert c.execute("select count(*) from projetos.projeto").fetchone()[0] == 0
