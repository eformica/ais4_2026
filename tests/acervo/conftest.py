import hashlib
import textwrap

import pytest
from fastapi.testclient import TestClient

from ais4 import auth, config
from ais4.acervo.rotas import obter_embedder
from ais4.acervo.similaridade import EmbedderFalso
from ais4.app import criar_app


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def embedder_falso() -> EmbedderFalso:
    """Embedder injetado nas rotas; os testes podem preencher `.vetores`."""
    return EmbedderFalso()


@pytest.fixture
def cliente(banco, limpar, tmp_path, monkeypatch, embedder_falso):
    arquivo = tmp_path / "usuarios.toml"
    arquivo.write_text(
        textwrap.dedent(
            f"""
            [[usuario]]
            nome = "ana"
            token_sha256 = "{_sha("t-ana")}"
            papeis = ["dono_do_projeto"]

            [[usuario]]
            nome = "bruno"
            token_sha256 = "{_sha("t-bruno")}"
            papeis = ["dono_do_projeto"]

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
    app = criar_app()
    app.dependency_overrides[obter_embedder] = lambda: embedder_falso
    yield TestClient(app)
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()
