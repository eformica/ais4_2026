"""Fronteira do workspace: o host nunca segue links plantados pelo código do agente.

Não usam Docker. Links simbólicos de arquivo exigem privilégio no Windows (os testes são pulados
sem ele); junctions de diretório não exigem e cobrem o mesmo caminho de código.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from ais4.comum.dto import ArtefatoRef
from ais4.execucao import sandbox as modulo
from ais4.execucao.artefatos import Armazem
from ais4.execucao.ferramentas import ContextoTarefa, materializar_entradas, montar_ferramentas
from ais4.execucao.sandbox import ErroWorkspace, Sandbox, verificar_destino
from tests.execucao.test_ferramentas import POR_CODIGO
from tests.execucao.test_sandbox_sem_docker import DockerFalso


def _ligar_pasta(link: Path, alvo: Path) -> None:
    """Link simbólico ou, no Windows sem privilégio, junction para um diretório."""
    alvo.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(alvo, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(alvo)], capture_output=True, check=False
        )
        if r.returncode != 0:
            pytest.skip("não foi possível criar link nem junction nesta conta")


def _ligar_arquivo(link: Path, alvo: Path) -> None:
    try:
        os.symlink(alvo, link)
    except OSError:
        pytest.skip("criar link simbólico de arquivo não é permitido nesta conta (Windows)")


@pytest.fixture
def docker_falso(monkeypatch) -> DockerFalso:
    falso = DockerFalso()
    monkeypatch.setattr(modulo.subprocess, "run", falso)
    return falso


def _contexto(tmp_path, **kw) -> ContextoTarefa:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    campos = {
        "projeto_id": uuid4(),
        "tarefa_id": uuid4(),
        "etapa": POR_CODIGO["harmonizacao"],
        "workspace": workspace,
        "sandbox": Sandbox(),
        "armazem": Armazem(tmp_path / "armazem"),
        "semente": 1,
    }
    campos.update(kw)
    return ContextoTarefa(**campos)


def _ref(contexto, dados: bytes) -> ArtefatoRef:
    uri, sha = contexto.armazem.salvar(dados)
    return ArtefatoRef(uri=uri, sha256=sha, nome="x", tipo="outro")


# --- scripts --------------------------------------------------------------------------------
def test_scripts_virou_link_nao_escreve_fora_do_workspace(tmp_path, docker_falso):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fora = tmp_path / "fora"
    _ligar_pasta(workspace / "_scripts", fora)

    with pytest.raises(ErroWorkspace, match="_scripts"):
        Sandbox().executar(workspace, "print(1)", rede=False, semente=1)

    assert list(fora.iterdir()) == []
    assert not any(c[:2] == ["docker", "run"] for c in docker_falso.comandos)


def test_executar_python_devolve_erro_ao_modelo_quando_ha_link(tmp_path, docker_falso):
    contexto = _contexto(tmp_path)
    fora = tmp_path / "fora"
    _ligar_pasta(contexto.workspace / "_scripts", fora)
    executar = next(f for f in montar_ferramentas(contexto) if f.name == "executar_python")

    texto = executar(codigo="print(1)")

    assert texto.startswith("Erro") and "link" in texto
    assert list(fora.iterdir()) == [] and contexto.scripts == []


def test_script_numerado_nao_sobrescreve_arquivo_existente(tmp_path, docker_falso):
    workspace = tmp_path / "ws"
    (workspace / "_scripts").mkdir(parents=True)
    (workspace / "_scripts" / "_script_1.py").write_text("antigo")

    r = Sandbox().executar(workspace, "novo", rede=False, semente=1)

    assert r.script.name == "_script_2.py"
    assert (workspace / "_scripts" / "_script_1.py").read_text() == "antigo"


def test_script_e_gravado_em_utf8(tmp_path, docker_falso):
    r = Sandbox().executar(tmp_path / "ws", "print('açaí ✓')", rede=False, semente=1)
    assert r.script.read_bytes() == "print('açaí ✓')".encode()


def test_script_de_link_de_arquivo_plantado_e_recusado(tmp_path, docker_falso):
    workspace = tmp_path / "ws"
    (workspace / "_scripts").mkdir(parents=True)
    fora = tmp_path / "alvo.py"
    fora.write_text("intocado")
    _ligar_arquivo(workspace / "_scripts" / "_script_1.py", fora)

    r = Sandbox().executar(workspace, "novo", rede=False, semente=1)

    assert fora.read_text() == "intocado"  # nunca seguiu o link
    assert r.script.name == "_script_2.py"


def test_verificar_destino_rejeita_fora_e_traversal(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert verificar_destino(workspace, workspace / "a" / "b.txt") == (workspace / "a" / "b.txt")
    for alvo in (tmp_path / "outro.txt", workspace / ".." / "outro.txt"):
        with pytest.raises(ErroWorkspace):
            verificar_destino(workspace, alvo)


# --- entradas -------------------------------------------------------------------------------
def test_entradas_virou_link_e_rejeitado_sem_escrever_fora(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["dados"] = _ref(contexto, b"conteudo")
    fora = tmp_path / "fora"
    _ligar_pasta(contexto.workspace / "entradas", fora)

    with pytest.raises(ValueError, match="entradas"):
        materializar_entradas(contexto)

    assert list(fora.iterdir()) == []


def test_entradas_nome_plantado_como_link_de_arquivo_e_rejeitado(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["dados"] = _ref(contexto, b"conteudo")
    (contexto.workspace / "entradas").mkdir()
    fora = tmp_path / "alvo.txt"
    fora.write_text("intocado")
    _ligar_arquivo(contexto.workspace / "entradas" / "dados", fora)

    with pytest.raises(ValueError, match="link"):
        materializar_entradas(contexto)

    assert fora.read_text() == "intocado"


def test_entradas_reaproveita_workspace_substituindo_arquivo_comum(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["dados"] = _ref(contexto, b"novo")
    (contexto.workspace / "entradas").mkdir()
    (contexto.workspace / "entradas" / "dados").write_bytes(b"velho e mais longo")

    (caminho,) = materializar_entradas(contexto)

    assert caminho.read_bytes() == b"novo"


def test_entradas_nome_que_e_diretorio_e_rejeitado(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["dados"] = _ref(contexto, b"x")
    (contexto.workspace / "entradas" / "dados").mkdir(parents=True)
    with pytest.raises(ValueError, match="diretório"):
        materializar_entradas(contexto)


# --- limpeza do container e endurecimento ---------------------------------------------------
def test_qualquer_excecao_no_run_mata_o_container(tmp_path, monkeypatch):
    falso = DockerFalso(ao_rodar=lambda _c: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(modulo.subprocess, "run", falso)

    with pytest.raises(KeyboardInterrupt):
        Sandbox().executar(tmp_path, "x", rede=False, semente=1)

    nome = falso.run_cmd()[falso.run_cmd().index("--name") + 1]
    assert ["docker", "kill", nome] in falso.comandos


def test_execucao_normal_nao_chama_kill(tmp_path, docker_falso):
    Sandbox().executar(tmp_path, "x", rede=False, semente=1)
    assert not any(c[:2] == ["docker", "kill"] for c in docker_falso.comandos)


def test_falha_do_kill_e_ignorada(tmp_path, monkeypatch):
    base = DockerFalso(expirar=True)

    def run(cmd, **kw):
        if cmd[:2] == ["docker", "kill"]:
            assert kw["timeout"] > 0  # o kill tem timeout próprio
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return base(cmd, **kw)

    monkeypatch.setattr(modulo.subprocess, "run", run)
    r = Sandbox().executar(tmp_path, "x", rede=False, timeout_s=1, semente=1)
    assert r.codigo_saida == 124


def test_flags_de_endurecimento_e_registro_no_ambiente(tmp_path, docker_falso):
    r = Sandbox().executar(tmp_path, "x", rede=False, semente=1)
    run = docker_falso.run_cmd()

    assert run[run.index("--cap-drop") + 1] == "ALL"
    assert run[run.index("--security-opt") + 1] == "no-new-privileges"
    assert run[run.index("--pids-limit") + 1] == "512"
    assert run[run.index("--memory") + 1] == run[run.index("--memory-swap") + 1] == "4g"
    assert run[run.index("--user") + 1] == "1000:1000"
    assert r.ambiente["seguranca"] == {
        "cap_drop": "ALL",
        "no_new_privileges": True,
        "pids_limit": 512,
        "memoria": "4g",
        "memoria_swap": "4g",
        "cpus": "2",
        "usuario": "1000:1000",
    }


def test_usuario_none_omite_user(tmp_path, docker_falso):
    r = Sandbox(usuario=None).executar(tmp_path, "x", rede=False, semente=1)
    assert "--user" not in docker_falso.run_cmd()
    assert r.ambiente["seguranca"]["usuario"] is None


# --- concluir_etapa nunca levanta -----------------------------------------------------------
def _concluir(contexto):
    return next(f for f in montar_ferramentas(contexto) if f.name == "concluir_etapa")


def test_concluir_etapa_json_aninhado_demais_nao_levanta(tmp_path):
    contexto = _contexto(tmp_path)
    msg = _concluir(contexto)(artefatos_json="[" * 200000, resumo="x")
    assert msg.startswith("Erro") and contexto.saida is None


def test_concluir_etapa_oserror_vira_erro_para_o_modelo(tmp_path, monkeypatch):
    contexto = _contexto(tmp_path)
    (contexto.workspace / "a.txt").write_text("x")

    def quebra(_dados):
        raise OSError("disco cheio")

    monkeypatch.setattr(contexto.armazem, "salvar", quebra)
    msg = _concluir(contexto)(artefatos_json=json.dumps({"a": "a.txt"}), resumo="x")

    assert msg.startswith("Erro") and "disco cheio" in msg and contexto.saida is None


def test_concluir_etapa_link_para_fora_do_workspace_e_rejeitado(tmp_path):
    contexto = _contexto(tmp_path)
    fora = tmp_path / "segredo.txt"
    fora.write_text("s")
    _ligar_arquivo(contexto.workspace / "atalho.txt", fora)

    msg = _concluir(contexto)(artefatos_json='{"a": "atalho.txt"}', resumo="x")

    assert msg.startswith("Erro") and contexto.saida is None


def test_verificar_destino_rejeita_link_no_ultimo_componente(tmp_path, monkeypatch):
    """Simula um link de arquivo plantado (não exige privilégio de symlink no Windows)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    alvo = workspace / "plantado.txt"
    original = modulo.e_ligacao
    monkeypatch.setattr(modulo, "e_ligacao", lambda p: Path(p) == alvo or original(p))

    with pytest.raises(ErroWorkspace, match="plantado.txt"):
        verificar_destino(workspace, alvo)
