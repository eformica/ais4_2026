"""Testes com Docker real (`-m docker`). Sem Docker ou sem a imagem, são pulados."""

import subprocess

import pytest

from ais4.execucao.sandbox import ErroWorkspace, Sandbox

pytestmark = pytest.mark.docker

IMAGEM = "ais4-sandbox:0.1.0"


@pytest.fixture(scope="module", autouse=True)
def _docker_com_imagem():
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", IMAGEM], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker indisponível")
    if r.returncode != 0:
        pytest.skip(f"imagem {IMAGEM} ausente: docker build -t {IMAGEM} sandbox/")


@pytest.fixture
def sandbox() -> Sandbox:
    return Sandbox()


def test_sandbox_executa_e_registra_ambiente(sandbox, tmp_path):
    r = sandbox.executar(
        tmp_path, "import pandas; print(pandas.__version__)", rede=False, semente=7
    )

    assert r.codigo_saida == 0 and r.stdout.strip() == "3.0.3"
    assert r.ambiente["imagem"] == IMAGEM
    assert r.ambiente["imagem_digest"].startswith("sha256:")
    assert (r.ambiente["semente"], r.ambiente["rede"]) == (7, False)


def test_sandbox_semente_visivel_no_ambiente(sandbox, tmp_path):
    codigo = "import os; print(os.environ['AIS4_SEMENTE'], os.environ['PYTHONHASHSEED'])"
    r = sandbox.executar(tmp_path, codigo, rede=False, semente=123)
    assert r.stdout.split() == ["123", "123"]


def test_sandbox_sem_rede(sandbox, tmp_path):
    codigo = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), 2)\n"
        "    print('CONECTOU')\n"
        "except OSError:\n"
        "    print('SEM_REDE')\n"
    )
    r = sandbox.executar(tmp_path, codigo, rede=False, semente=1)
    assert r.stdout.strip() == "SEM_REDE"


def test_sandbox_timeout_vira_124_e_mata_o_container(sandbox, tmp_path):
    r = sandbox.executar(
        tmp_path, "import time; time.sleep(60)", rede=False, timeout_s=3, semente=1
    )

    assert r.codigo_saida == 124
    vivos = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=ais4-sandbox-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert vivos == ""


def test_sandbox_detecta_arquivos_novos_e_modificados(sandbox, tmp_path):
    (tmp_path / "velho.txt").write_text("v1")
    (tmp_path / "intacto.txt").write_text("igual")
    codigo = (
        "open('/work/novo.csv', 'w').write('a,b\\n1,2\\n')\n"
        "import os; os.makedirs('/work/saida', exist_ok=True)\n"
        "open('/work/saida/g.png', 'wb').write(b'x')\n"
        "open('/work/velho.txt', 'w').write('v2 mais longo')\n"
    )
    r = sandbox.executar(tmp_path, codigo, rede=False, semente=1)

    assert r.codigo_saida == 0
    assert sorted(p.as_posix() for p in r.arquivos_novos) == [
        "novo.csv",
        "saida/g.png",
        "velho.txt",
    ]


def test_sandbox_guarda_cada_script_numerado(sandbox, tmp_path):
    r1 = sandbox.executar(tmp_path, "print(1)", rede=False, semente=1)
    r2 = sandbox.executar(tmp_path, "print(2)", rede=False, semente=1)

    assert (tmp_path / "_scripts" / "_script_1.py").read_text() == "print(1)"
    assert (tmp_path / "_scripts" / "_script_2.py").read_text() == "print(2)"
    assert (r1.script.name, r2.script.name) == ("_script_1.py", "_script_2.py")
    # os scripts não contam como arquivos novos
    assert r2.arquivos_novos == []


def test_sandbox_trunca_saida_nos_ultimos_20000(sandbox, tmp_path):
    r = sandbox.executar(tmp_path, "print('a' * 30000 + 'FIM')", rede=False, semente=1)
    assert len(r.stdout) == 20000 and r.stdout.rstrip().endswith("FIM")


def test_sandbox_erro_do_script_volta_no_stderr(sandbox, tmp_path):
    r = sandbox.executar(tmp_path, "raise SystemExit('quebrou')", rede=False, semente=1)
    assert r.codigo_saida == 1 and "quebrou" in r.stderr


def test_sandbox_com_rede_bridge_tem_interface_de_rede(sandbox, tmp_path):
    codigo = "import socket; print(socket.gethostbyname('localhost'))"
    r = sandbox.executar(tmp_path, codigo, rede=True, semente=1)
    assert r.codigo_saida == 0 and r.ambiente["rede"] is True


def test_sandbox_script_que_planta_link_em_scripts_bloqueia_a_proxima_execucao(sandbox, tmp_path):
    codigo = (
        "import os, shutil\nshutil.rmtree('/work/_scripts')\nos.symlink('/tmp', '/work/_scripts')\n"
    )
    r1 = sandbox.executar(tmp_path, codigo, rede=False, semente=1)
    assert r1.codigo_saida == 0, r1.stderr

    with pytest.raises(ErroWorkspace, match="_scripts"):
        sandbox.executar(tmp_path, "print('nao deve rodar')", rede=False, semente=1)


def test_sandbox_roda_sem_root_e_escreve_no_workspace(sandbox, tmp_path):
    r = sandbox.executar(tmp_path, "import os; print(os.getuid())", rede=False, semente=1)
    assert r.stdout.strip() == "1000"
    assert r.ambiente["seguranca"]["usuario"] == "1000:1000"
