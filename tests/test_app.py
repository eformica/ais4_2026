import hashlib
import textwrap
from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from ais4 import __main__ as cli
from ais4 import auth, config
from ais4.app import criar_app
from ais4.auth import Usuario, exigir_papel, usuario_atual
from ais4.comum.erros import NaoEncontrado, RegraViolada

TOKEN_CARLA = "token-da-carla"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def usuarios_toml(tmp_path, monkeypatch):
    def escrever(conteudo: str):
        arquivo = tmp_path / "usuarios.toml"
        arquivo.write_text(textwrap.dedent(conteudo), encoding="utf-8")
        monkeypatch.setenv("AIS4_USUARIOS_ARQUIVO", str(arquivo))
        config.obter_config.cache_clear()
        auth.carregar_usuarios.cache_clear()
        return arquivo

    escrever(
        f"""
        [[usuario]]
        nome = "carla"
        token_sha256 = "{_sha(TOKEN_CARLA)}"
        papeis = ["curador_do_acervo"]

        [[usuario]]
        nome = "davi"
        token_sha256 = "{_sha("token-do-davi")}"
        papeis = ["dono_do_projeto"]
        """
    )
    yield escrever
    config.obter_config.cache_clear()
    auth.carregar_usuarios.cache_clear()


@pytest.fixture
def cliente(usuarios_toml):
    app = criar_app()

    @app.get("/_teste/quem")
    def quem(usuario: Annotated[Usuario, Depends(usuario_atual)]):
        return {"nome": usuario.nome, "papeis": sorted(usuario.papeis)}

    @app.get("/_teste/curador")
    def curador(usuario: Annotated[Usuario, Depends(exigir_papel("curador_do_acervo"))]):
        return {"nome": usuario.nome}

    @app.get("/_teste/regra")
    def regra():
        raise RegraViolada("regra quebrada")

    @app.get("/_teste/nada")
    def nada():
        raise NaoEncontrado("não existe")

    return TestClient(app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_saude_sem_token_ok(cliente):
    r = cliente.get("/saude")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_rota_protegida_sem_token_401(cliente):
    r = cliente.get("/_teste/quem")
    assert r.status_code == 401
    assert "erro" in r.json()


def test_token_invalido_401(cliente):
    assert cliente.get("/_teste/quem", headers=_bearer("errado")).status_code == 401
    # esquema que não é Bearer
    assert (
        cliente.get("/_teste/quem", headers={"Authorization": f"Basic {TOKEN_CARLA}"}).status_code
        == 401
    )


def test_token_valido_identifica_usuario(cliente):
    r = cliente.get("/_teste/quem", headers=_bearer(TOKEN_CARLA))
    assert r.status_code == 200
    assert r.json() == {"nome": "carla", "papeis": ["curador_do_acervo"]}


def test_papel_ausente_403(cliente):
    r = cliente.get("/_teste/curador", headers=_bearer("token-do-davi"))
    assert r.status_code == 403
    assert "erro" in r.json()


def test_papel_presente_ok(cliente):
    r = cliente.get("/_teste/curador", headers=_bearer(TOKEN_CARLA))
    assert r.status_code == 200


def test_regra_violada_vira_409(cliente):
    r = cliente.get("/_teste/regra")
    assert r.status_code == 409
    assert r.json() == {"erro": "regra quebrada"}


def test_nao_encontrado_vira_404(cliente):
    r = cliente.get("/_teste/nada")
    assert r.status_code == 404
    assert r.json() == {"erro": "não existe"}


def test_papel_desconhecido_rejeitado_ao_carregar(usuarios_toml):
    usuarios_toml(
        f"""
        [[usuario]]
        nome = "eva"
        token_sha256 = "{_sha("x")}"
        papeis = ["rei_do_mundo"]
        """
    )
    with pytest.raises(ValueError, match="rei_do_mundo"):
        auth.carregar_usuarios(config.obter_config().usuarios_arquivo)


def test_exemplo_de_usuarios_e_valido():
    exemplo = config.Config(usuarios_arquivo="config/usuarios.exemplo.toml")
    usuarios = auth.carregar_usuarios(exemplo.usuarios_arquivo)
    assert usuarios
    assert all(u.papeis <= auth.PAPEIS for u in usuarios.values())


def test_cli_subcomando_nao_implementado_sai_com_2(capsys):
    assert cli.main(["worker-outbox"]) == 2
    assert "não implementado" in capsys.readouterr().err


def test_cli_exige_subcomando():
    with pytest.raises(SystemExit) as saida:
        cli.main([])
    assert saida.value.code == 2


def test_cli_migrar_chama_alembic_upgrade_head(monkeypatch):
    chamadas = []
    monkeypatch.setattr(cli.command, "upgrade", lambda cfg, rev: chamadas.append((cfg, rev)))
    assert cli.main(["migrar"]) == 0
    ((cfg, rev),) = chamadas
    assert rev == "head"
    assert cfg.config_file_name.endswith("alembic.ini")
    assert cfg.get_main_option("script_location").endswith("migracoes")


def test_cli_api_sobe_uvicorn_com_factory(monkeypatch):
    chamadas = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: chamadas.append((a, kw)))
    assert cli.main(["api", "--porta", "9000"]) == 0
    ((args, kwargs),) = chamadas
    assert args == ("ais4.app:criar_app",)
    assert kwargs["factory"] is True
    assert kwargs["port"] == 9000
