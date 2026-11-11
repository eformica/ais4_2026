import hashlib
import textwrap

import pytest
from fastapi.testclient import TestClient

from ais4 import auth, config
from ais4.app import criar_app
from ais4.playbooks import servico
from tests.playbooks.test_servico import PlaybookDeTeste

SEMENTE = "00000000-0000-4000-8000-000000000001"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def cliente(banco, limpar, tmp_path, monkeypatch):
    arquivo = tmp_path / "usuarios.toml"
    arquivo.write_text(
        textwrap.dedent(
            f"""
            [[usuario]]
            nome = "bruno"
            token_sha256 = "{_sha("t-bruno")}"
            papeis = ["guardiao_de_playbooks"]

            [[usuario]]
            nome = "carla"
            token_sha256 = "{_sha("t-carla")}"
            papeis = ["curador_do_acervo"]
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIS4_USUARIOS_ARQUIVO", str(arquivo))
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()
    yield TestClient(criar_app())
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()


def test_listar_exige_autenticacao(cliente):
    assert cliente.get("/playbooks").status_code == 401


def test_listar_qualquer_usuario_autenticado(cliente):
    r = cliente.get("/playbooks", headers=_bearer("t-carla"))
    assert r.status_code == 200
    assert r.json() == [
        {
            "playbook_id": SEMENTE,
            "codigo": "projeto-academico-simples",
            "versao": "0.1.0",
            "status": "experimental",
        }
    ]


def test_detalhe_da_versao_com_etapas(cliente):
    r = cliente.get(
        "/playbooks/projeto-academico-simples/versoes/0.1.0", headers=_bearer("t-carla")
    )
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["versao"]["status"] == "experimental"
    assert [e["codigo"] for e in corpo["etapas"]][:2] == ["contexto", "coleta_dados"]


def test_detalhe_inexistente_404(cliente):
    r = cliente.get("/playbooks/nao-existe/versoes/0.1.0", headers=_bearer("t-carla"))
    assert r.status_code == 404
    assert "erro" in r.json()


def test_alterar_status_exige_guardiao(cliente):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    url = "/playbooks/playbook-de-teste/versoes/0.1.0/status"
    r = cliente.post(url, json={"status": "experimental"}, headers=_bearer("t-carla"))
    assert r.status_code == 403


def test_alterar_status_pelo_guardiao(cliente):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    url = "/playbooks/playbook-de-teste/versoes/0.1.0/status"
    r = cliente.post(url, json={"status": "experimental"}, headers=_bearer("t-bruno"))
    assert r.status_code == 200
    assert r.json()["status"] == "experimental"
    assert (
        cliente.get(url.removesuffix("/status"), headers=_bearer("t-bruno")).json()["versao"][
            "status"
        ]
        == "experimental"
    )


def test_alterar_status_do_proprio_proponente_403(cliente):
    servico.sincronizar(PlaybookDeTeste(), "bruno")
    url = "/playbooks/playbook-de-teste/versoes/0.1.0/status"
    r = cliente.post(url, json={"status": "experimental"}, headers=_bearer("t-bruno"))
    assert r.status_code == 403


def test_alterar_status_invalido_422_e_transicao_invalida_409(cliente):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    url = "/playbooks/playbook-de-teste/versoes/0.1.0/status"
    assert cliente.post(url, json={"status": "xyz"}, headers=_bearer("t-bruno")).status_code == 422
    r = cliente.post(url, json={"status": "validado"}, headers=_bearer("t-bruno"))
    assert r.status_code == 409


def test_alterar_status_inexistente_404(cliente):
    r = cliente.post(
        "/playbooks/nao-existe/versoes/0.1.0/status",
        json={"status": "experimental"},
        headers=_bearer("t-bruno"),
    )
    assert r.status_code == 404
