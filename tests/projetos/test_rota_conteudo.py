"""GET /projetos/{id}/artefatos/{aid}/conteudo: stream do arquivo do armazém."""

from uuid import uuid4

import pytest

from ais4.comum.dto import ArtefatoRef
from ais4.execucao.artefatos import Armazem
from ais4.projetos import rotas, servico
from tests.projetos.conftest import cabecalho
from tests.projetos.fabrica import novo_projeto


@pytest.fixture
def armazem(cliente, tmp_path) -> Armazem:
    armazem = Armazem(tmp_path / "armazem")
    cliente.app.dependency_overrides[rotas.obter_armazem] = lambda: armazem
    yield armazem
    cliente.app.dependency_overrides.pop(rotas.obter_armazem, None)


def _registrar(armazem, projeto_id, dados=b"a,b\n1,2\n", nome="tabela.csv") -> str:
    uri, sha = armazem.salvar(dados)
    ref = ArtefatoRef(uri=uri, sha256=sha, nome=nome, tipo="dataset")
    servico.registrar_artefato(projeto_id, None, "dataset", nome, ref, {})
    return str(next(a.id for a in servico.artefatos(projeto_id) if a.nome == nome))


def test_conteudo_devolve_os_bytes_armazenados(cliente, armazem):
    p = novo_projeto()
    aid = _registrar(armazem, p.id)

    r = cliente.get(f"/projetos/{p.id}/artefatos/{aid}/conteudo", headers=cabecalho("fred"))

    assert r.status_code == 200 and r.content == b"a,b\n1,2\n"
    assert "tabela.csv" in r.headers["content-disposition"]


def test_conteudo_exige_autenticacao(cliente, armazem):
    p = novo_projeto()
    aid = _registrar(armazem, p.id)
    assert cliente.get(f"/projetos/{p.id}/artefatos/{aid}/conteudo").status_code == 401


def test_conteudo_adulterado_409(cliente, armazem):
    p = novo_projeto()
    aid = _registrar(armazem, p.id)
    (armazem_arquivo,) = [f for f in armazem.raiz.rglob("*") if f.is_file()]
    armazem_arquivo.write_bytes(b"adulterado")

    r = cliente.get(f"/projetos/{p.id}/artefatos/{aid}/conteudo", headers=cabecalho("ana"))

    assert r.status_code == 409 and "artefato adulterado" in r.json()["erro"]


def test_conteudo_de_outro_projeto_ou_inexistente_404(cliente, armazem):
    p = novo_projeto()
    q = novo_projeto(titulo="Outro")
    aid = _registrar(armazem, p.id)
    h = cabecalho("ana")

    assert cliente.get(f"/projetos/{q.id}/artefatos/{aid}/conteudo", headers=h).status_code == 404
    assert (
        cliente.get(f"/projetos/{p.id}/artefatos/{uuid4()}/conteudo", headers=h).status_code == 404
    )
    assert (
        cliente.get(f"/projetos/{uuid4()}/artefatos/{aid}/conteudo", headers=h).status_code == 404
    )


def test_conteudo_com_uri_fora_do_armazem_404(cliente, armazem, tmp_path):
    p = novo_projeto()
    fora = tmp_path / "fora.txt"
    fora.write_text("x")
    ref = ArtefatoRef(uri=fora.as_uri(), sha256="0" * 64, nome="fora", tipo="outro")
    servico.registrar_artefato(p.id, None, "outro", "fora", ref, {})
    aid = servico.artefatos(p.id)[0].id

    r = cliente.get(f"/projetos/{p.id}/artefatos/{aid}/conteudo", headers=cabecalho("ana"))

    assert r.status_code == 404
