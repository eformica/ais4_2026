import psycopg
import pytest
import sqlalchemy.exc
from sqlalchemy import text

from ais4.comum.erros import RegraViolada, traduzir
from ais4.db import engine, sessao

SEMENTE_IDEIA = "00000000-0000-4000-8000-000000000101"
MODULOS = ("playbooks", "acervo", "projetos", "criacao", "execucao")


def test_acervo_app_nao_le_projetos(banco, limpar):
    with pytest.raises(sqlalchemy.exc.ProgrammingError), engine("acervo").connect() as c:
        c.execute(text("select 1 from projetos.projeto"))  # permission denied


def test_engine_loga_como_papel_do_modulo(banco):
    for modulo in MODULOS:
        with engine(modulo).connect() as c:
            assert c.execute(text("select current_user")).scalar_one() == f"{modulo}_app"


def test_autor_registrado_no_evento(banco, limpar):
    with sessao("acervo", "maria") as s:
        s.execute(
            text("update acervo.ideia set status='rascunho' where id=:i"), {"i": SEMENTE_IDEIA}
        )
    with sessao("acervo", "x") as s:
        autor = s.execute(
            text("select autor from acervo.ideia_evento where para_status='rascunho'")
        ).scalar_one()
    assert autor == "maria"


def test_transicao_invalida_vira_regra_violada(banco, limpar):
    with (
        pytest.raises(RegraViolada, match="Transição de status de ideia inválida"),
        sessao("acervo", "maria") as s,
    ):
        s.execute(
            text("update acervo.ideia set status='concluida' where id=:i"),
            {"i": SEMENTE_IDEIA},
        )


def test_unicidade_vira_regra_violada(banco, limpar):
    with pytest.raises(RegraViolada), sessao("playbooks", "maria") as s:
        s.execute(text("insert into playbooks.playbook select * from playbooks.playbook"))


def test_erro_nao_mapeado_passa_como_dbapierror(banco, limpar):
    with pytest.raises(sqlalchemy.exc.DBAPIError) as info, sessao("acervo", "maria") as s:
        s.execute(text("select 1 from projetos.projeto"))
    assert not isinstance(info.value, RegraViolada)


def test_rollback_em_excecao(banco, limpar):
    with pytest.raises(RuntimeError), sessao("acervo", "maria") as s:
        s.execute(
            text("update acervo.ideia set status='rascunho' where id=:i"),
            {"i": SEMENTE_IDEIA},
        )
        raise RuntimeError("falha")
    with sessao("acervo", "x") as s:
        status = s.execute(
            text("select status from acervo.ideia where id=:i"), {"i": SEMENTE_IDEIA}
        ).scalar_one()
    assert status == "triagem"


def test_traduzir_devolve_a_propria_excecao_se_nao_mapeada():
    exc = sqlalchemy.exc.DBAPIError("select 1", None, ValueError("x"))
    assert traduzir(exc) is exc


def test_app_nao_apaga_linhas(banco, limpar):
    with pytest.raises(sqlalchemy.exc.ProgrammingError), engine("acervo").begin() as c:
        c.execute(text("delete from acervo.ideia"))


def test_papeis_app_podem_logar_e_nao_sao_super(banco):
    with psycopg.connect(banco) as c:
        linhas = c.execute(
            r"select rolname, rolcanlogin, rolsuper from pg_roles where rolname like '%\_app'"
        ).fetchall()
    assert {r[0] for r in linhas} == {f"{m}_app" for m in MODULOS}
    assert all(r[1] and not r[2] for r in linhas)
