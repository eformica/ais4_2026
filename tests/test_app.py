import hashlib
import json
import signal
import textwrap
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from ais4 import __main__ as cli
from ais4 import auth, config
from ais4.app import criar_app
from ais4.auth import Usuario, exigir_papel, usuario_atual
from ais4.comum.erros import NaoEncontrado, RegraViolada
from ais4.processos.atualizacao_acervo import RelatorioRevisao

TOKEN_CARLA = "token-da-carla"


@pytest.fixture(autouse=True)
def sem_sincronizacao_de_playbooks(monkeypatch):
    """Os testes de CLI daqui não têm banco; a sincronização na subida tem testes próprios
    (tests/test_cli_inicializacao.py)."""
    monkeypatch.setattr(cli, "_sincronizar_playbooks", lambda: 0)


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


def test_cli_nao_ha_subcomando_nao_implementado():
    assert not hasattr(cli, "NAO_IMPLEMENTADOS")
    ajuda = cli._parser().format_help()
    for comando in ("api", "migrar", "worker-outbox", "motor-criacao", "worker-execucao"):
        assert comando in ajuda
    assert "revisao-acervo" in ajuda


def test_cli_revisao_acervo_imprime_o_relatorio_em_json(monkeypatch, capsys):
    a, b, lacuna = uuid4(), uuid4(), uuid4()
    chamadas = []

    def revisao_falsa(usuario, embedder):
        chamadas.append((usuario, embedder))
        return RelatorioRevisao(2, [(a, b, 0.91)], [lacuna], [])

    monkeypatch.setattr(cli.atualizacao_acervo, "revisao_periodica", revisao_falsa)
    monkeypatch.setattr(cli, "VoyageEmbedder", lambda: "embedder-voyage")

    assert cli.main(["revisao-acervo"]) == 0

    assert chamadas == [("sistema:revisao-acervo", "embedder-voyage")]
    saida = json.loads(capsys.readouterr().out)
    assert saida == {
        "reindexadas": 2,
        "candidatas_fusao": [[str(a), str(b), 0.91]],
        "lacunas_reabertas": [str(lacuna)],
        "falhas_indexacao": [],
    }


def test_cli_worker_execucao_roda_o_loop_e_para_no_sigterm(monkeypatch):
    antes = signal.getsignal(signal.SIGTERM)
    vistos = {}

    def loop_falso(cliente, intervalo_s, parar=None, **kw):
        vistos["args"] = (cliente, intervalo_s)
        vistos["antes"] = parar.is_set()
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # o que o SIGTERM faria
        vistos["depois"] = parar.is_set()

    monkeypatch.setattr(cli, "ClienteAnthropic", lambda: "cliente")
    monkeypatch.setattr(cli.orquestrador, "loop", loop_falso)

    assert cli.main(["worker-execucao", "--intervalo", "3"]) == 0

    assert vistos == {"args": ("cliente", 3.0), "antes": False, "depois": True}
    assert signal.getsignal(signal.SIGTERM) == antes


def test_cli_worker_execucao_trata_keyboardinterrupt_como_saida_limpa(monkeypatch):
    def loop_falso(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "ClienteAnthropic", lambda: "cliente")
    monkeypatch.setattr(cli.orquestrador, "loop", loop_falso)
    assert cli.main(["worker-execucao"]) == 0


def test_cli_worker_outbox_carrega_handlers_e_roda_o_loop(monkeypatch):
    chamadas = []
    monkeypatch.setattr(cli.eventos, "carregar_handlers", lambda: chamadas.append("carregar"))
    monkeypatch.setattr(cli.eventos, "loop", lambda *a, **k: chamadas.append("loop"))
    assert cli.main(["worker-outbox"]) == 0
    assert chamadas == ["carregar", "loop"]


def test_cli_motor_criacao_roda_o_loop_com_o_cliente_anthropic(monkeypatch):
    chamadas = []
    monkeypatch.setattr(cli, "ClienteAnthropic", lambda: "cliente")
    monkeypatch.setattr(
        cli.motor,
        "loop",
        lambda cliente, intervalo_s, parar=None, usuario_sistema=None: chamadas.append(
            (cliente, intervalo_s, usuario_sistema)
        ),
    )
    assert cli.main(["motor-criacao", "--intervalo", "7"]) == 0
    assert chamadas == [("cliente", 7.0, "sistema:motor-criacao")]


def test_cli_motor_criacao_sigterm_aciona_a_parada_e_restaura_os_handlers(monkeypatch):
    antes = signal.getsignal(signal.SIGTERM)
    vistos = {}

    def loop_falso(cliente, intervalo_s, parar=None, usuario_sistema=None):
        vistos["antes"] = parar.is_set()
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # o que o SIGTERM faria
        vistos["depois"] = parar.is_set()

    monkeypatch.setattr(cli, "ClienteAnthropic", lambda: "cliente")
    monkeypatch.setattr(cli.motor, "loop", loop_falso)

    assert cli.main(["motor-criacao"]) == 0

    assert vistos == {"antes": False, "depois": True}
    assert signal.getsignal(signal.SIGTERM) == antes


def test_cli_motor_criacao_trata_keyboardinterrupt_como_saida_limpa(monkeypatch):
    def loop_falso(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "ClienteAnthropic", lambda: "cliente")
    monkeypatch.setattr(cli.motor, "loop", loop_falso)
    assert cli.main(["motor-criacao"]) == 0


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
