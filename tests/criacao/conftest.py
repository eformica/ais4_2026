import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from ais4 import auth, config
from ais4.app import criar_app
from ais4.criacao.rotas import obter_cliente_llm
from ais4.llm.falso import ClienteLLMFalso

USUARIOS = {
    "ana": ["dono_do_projeto"],
    "tati": ["responsavel_tecnico"],
    "rafa": ["dono_do_projeto", "responsavel_tecnico"],
    "rita": ["revisor_metodologico"],
}


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cabecalho(nome: str) -> dict[str, str]:
    return {"Authorization": f"Bearer t-{nome}"}


@pytest.fixture
def llm_da_rota():
    """O `ClienteLLMFalso` que a API usa; cada teste atribui `llm_da_rota.cliente`."""

    class Holder:
        cliente: ClienteLLMFalso = ClienteLLMFalso({})

    return Holder()


@pytest.fixture
def api(banco, limpar, tmp_path, monkeypatch, llm_da_rota):
    arquivo = tmp_path / "usuarios.toml"
    blocos = [
        "[[usuario]]\n"
        f'nome = "{nome}"\n'
        f'token_sha256 = "{_sha("t-" + nome)}"\n'
        f"papeis = {json.dumps(papeis)}\n"
        for nome, papeis in USUARIOS.items()
    ]
    arquivo.write_text("\n".join(blocos), encoding="utf-8")
    monkeypatch.setenv("AIS4_USUARIOS_ARQUIVO", str(arquivo))
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()
    app = criar_app()
    app.dependency_overrides[obter_cliente_llm] = lambda: llm_da_rota.cliente
    yield TestClient(app)
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()
