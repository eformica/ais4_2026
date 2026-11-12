"""Promoção de playbook (spec 6.2): elegibilidade pelos projetos concluídos e a rota de promoção."""

import hashlib
import json
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from ais4 import auth, config
from ais4.app import criar_app
from ais4.playbooks import servico as playbooks
from ais4.processos.promocao import Elegibilidade, elegibilidade
from ais4.projetos import servico as projetos
from tests.playbooks.test_servico import PlaybookDeTeste
from tests.projetos.fabrica import novo_projeto, preparar_fechamento, usuario

SEMENTE = UUID("00000000-0000-4000-8000-000000000001")
USUARIOS = {
    "fred": ["guardiao_de_playbooks"],
    "bruno": ["guardiao_de_playbooks"],
    "carla": ["curador_do_acervo"],
}
URL = "/playbooks/projeto-academico-simples/versoes/0.1.0/promocao"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cabecalho(nome: str) -> dict[str, str]:
    return {"Authorization": f"Bearer t-{nome}"}


@pytest.fixture
def cliente(banco, limpar, tmp_path, monkeypatch):
    arquivo = tmp_path / "usuarios.toml"
    arquivo.write_text(
        "\n".join(
            f'[[usuario]]\nnome = "{nome}"\ntoken_sha256 = "{_sha("t-" + nome)}"\n'
            f"papeis = {json.dumps(papeis)}\n"
            for nome, papeis in USUARIOS.items()
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIS4_USUARIOS_ARQUIVO", str(arquivo))
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()
    yield TestClient(criar_app())
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()


def concluir_projeto(playbook_id: UUID, versao: str, *notas: int) -> UUID:
    """Projeto do playbook levado a `concluido`, com uma avaliação por nota (critérios distintos)."""
    p = novo_projeto(
        titulo=f"Projeto {len(projetos.listar())}",
        playbook_id=playbook_id,
        playbook_versao=versao,
    )
    preparar_fechamento(p.id)
    projetos.transicionar(p.id, "concluido", "ana")
    for i, nota in enumerate(notas):
        projetos.registrar_avaliacao(
            p.id, f"criterio_{i}", nota, None, usuario("rita", "revisor_metodologico")
        )
    return p.id


def status_da_versao(banco, codigo: str) -> str:
    with psycopg.connect(banco) as c:
        return c.execute(
            "select v.status from playbooks.playbook_versao v "
            "join playbooks.playbook p on p.id = v.playbook_id where p.codigo = %s",
            (codigo,),
        ).fetchone()[0]


def test_nao_elegivel_com_2_projetos(limpar):
    concluir_projeto(SEMENTE, "0.1.0", 5, 5)
    concluir_projeto(SEMENTE, "0.1.0", 5)

    e = elegibilidade(SEMENTE, "0.1.0")

    assert e == Elegibilidade(False, 2, 5.0, e.motivo)
    assert "2 projetos concluídos" in e.motivo and "mínimo de 3" in e.motivo


def test_sem_projetos_nao_e_elegivel_e_a_nota_media_e_nula(limpar):
    e = elegibilidade(SEMENTE, "0.1.0")

    assert (e.elegivel, e.projetos_concluidos, e.nota_media) == (False, 0, None)
    assert "0 projetos concluídos" in e.motivo


def test_nao_elegivel_com_nota_baixa(limpar):
    concluir_projeto(SEMENTE, "0.1.0", 5, 5)
    concluir_projeto(SEMENTE, "0.1.0", 5)
    fraco = concluir_projeto(SEMENTE, "0.1.0", 4, 3)  # média 3.5 < 4.0

    e = elegibilidade(SEMENTE, "0.1.0")

    assert (e.elegivel, e.projetos_concluidos) == (False, 3)
    assert e.nota_media == pytest.approx((5 + 5 + 3.5) / 3)
    assert str(fraco) in e.motivo and "3.50" in e.motivo and "4.00" in e.motivo


def test_nota_exatamente_no_limiar_e_elegivel(limpar):
    for _ in range(3):
        concluir_projeto(SEMENTE, "0.1.0", 4)

    e = elegibilidade(SEMENTE, "0.1.0")

    assert (e.elegivel, e.projetos_concluidos, e.nota_media) == (True, 3, 4.0)


def test_projeto_de_outra_versao_ou_nao_concluido_nao_conta(limpar):
    concluir_projeto(SEMENTE, "0.1.0", 5)
    concluir_projeto(SEMENTE, "0.1.0", 5)
    novo_projeto(titulo="Ainda em andamento")  # `aprovado`, sem avaliação

    assert elegibilidade(SEMENTE, "0.1.0").projetos_concluidos == 2
    assert elegibilidade(SEMENTE, "9.9.9").projetos_concluidos == 0


def test_elegivel_promove(cliente, banco):
    for notas in ((5, 4), (4,), (5,)):
        concluir_projeto(SEMENTE, "0.1.0", *notas)
    assert status_da_versao(banco, "projeto-academico-simples") == "experimental"

    r = cliente.post(URL, headers=cabecalho("fred"))

    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["versao"]["status"] == "validado"
    assert corpo["elegibilidade"]["elegivel"] is True
    assert corpo["elegibilidade"]["projetos_concluidos"] == 3
    assert corpo["elegibilidade"]["nota_media"] == pytest.approx((4.5 + 4 + 5) / 3)
    assert status_da_versao(banco, "projeto-academico-simples") == "validado"
    with psycopg.connect(banco) as c:
        evento = c.execute(  # o último: a semente também gerou o seu (-> experimental)
            "select payload->>'para', payload->>'autor' from playbooks.outbox "
            "where tipo_evento = 'PlaybookVersaoStatusAlterado' order by id desc limit 1"
        ).fetchone()
    assert evento == ("validado", "fred")


def test_promocao_nao_elegivel_e_409_com_o_motivo(cliente, banco):
    concluir_projeto(SEMENTE, "0.1.0", 5)

    r = cliente.post(URL, headers=cabecalho("fred"))

    assert r.status_code == 409
    assert "1 projetos concluídos" in r.json()["erro"]
    assert status_da_versao(banco, "projeto-academico-simples") == "experimental"


def test_promocao_exige_papel_guardiao_e_autenticacao(cliente):
    assert cliente.post(URL, headers=cabecalho("carla")).status_code == 403
    assert cliente.post(URL).status_code == 401


def test_promocao_de_versao_inexistente_e_404(cliente):
    r = cliente.post("/playbooks/nao-existe/versoes/0.1.0/promocao", headers=cabecalho("fred"))
    assert r.status_code == 404


def test_promocao_de_versao_que_nao_e_experimental_e_409(cliente, banco):
    for _ in range(3):
        concluir_projeto(SEMENTE, "0.1.0", 5)
    assert cliente.post(URL, headers=cabecalho("fred")).status_code == 200

    r = cliente.post(URL, headers=cabecalho("bruno"))  # já validado

    assert r.status_code == 409
    assert "experimentais" in r.json()["erro"]


def test_promocao_pelo_proponente_proibida(cliente, banco):
    playbooks.sincronizar(PlaybookDeTeste(), "fred")  # fred propõe a versão
    versao = playbooks.obter_versao("playbook-de-teste", "0.1.0")
    playbooks.alterar_status(
        versao.playbook_id, "0.1.0", "experimental", usuario("bruno", "guardiao_de_playbooks")
    )
    for _ in range(3):
        concluir_projeto(versao.playbook_id, "0.1.0", 5)
    assert elegibilidade(versao.playbook_id, "0.1.0").elegivel is True
    url = "/playbooks/playbook-de-teste/versoes/0.1.0/promocao"

    r = cliente.post(url, headers=cabecalho("fred"))

    assert r.status_code == 403
    assert "propôs" in r.json()["erro"]
    assert playbooks.obter_versao("playbook-de-teste", "0.1.0").status == "experimental"
    assert cliente.post(url, headers=cabecalho("bruno")).status_code == 200  # outro guardião pode
