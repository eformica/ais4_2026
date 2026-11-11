import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from ais4 import auth, config
from ais4.app import criar_app

USUARIOS = {
    "ana": ["dono_do_projeto"],
    "bruno": ["dono_do_projeto"],
    "davi": ["responsavel_por_dados"],
    "rita": ["revisor_metodologico"],
    "carla": ["curador_do_acervo"],
    "fred": ["guardiao_de_playbooks"],
}


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cabecalho(nome: str) -> dict[str, str]:
    return {"Authorization": f"Bearer t-{nome}"}


@pytest.fixture
def cliente(banco, limpar, tmp_path, monkeypatch):
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
    yield TestClient(criar_app())
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()
