"""Comportamento do Sandbox que não depende de Docker (usa um `docker` falso)."""

import subprocess

import pytest

from ais4.execucao import sandbox as modulo
from ais4.execucao.sandbox import Sandbox


class DockerFalso:
    """Substitui subprocess.run; registra os comandos e devolve respostas roteirizadas."""

    def __init__(self, *, digest="sha256:abc", ao_rodar=None, expirar=False):
        self.comandos: list[list[str]] = []
        self.digest = digest
        self.ao_rodar = ao_rodar
        self.expirar = expirar
        self._real = subprocess.run  # comandos que não são do docker passam direto

    def __call__(self, cmd, **kw):
        self.comandos.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, self.digest + "\n", "")
        if cmd[:2] == ["docker", "kill"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["docker", "run"]:
            if self.ao_rodar:
                self.ao_rodar(cmd)
            if self.expirar:
                raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b"parcial", stderr=b"")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        if cmd[0] != "docker":
            return self._real(cmd, **kw)
        raise AssertionError(cmd)

    def run_cmd(self) -> list[str]:
        return next(c for c in self.comandos if c[:2] == ["docker", "run"])


def test_comando_docker_run(tmp_path, monkeypatch):
    falso = DockerFalso()
    monkeypatch.setattr(modulo.subprocess, "run", falso)

    Sandbox().executar(tmp_path, "print(1)", rede=False, semente=9)

    run = falso.run_cmd()
    assert "--rm" in run
    assert run[run.index("--network") + 1] == "none"
    assert run[run.index("--memory") + 1] == "4g" and run[run.index("--cpus") + 1] == "2"
    assert f"{tmp_path.resolve()}:/work" in run
    assert "PYTHONHASHSEED=9" in run and "AIS4_SEMENTE=9" in run
    assert run[-2:] == ["python", "/work/_scripts/_script_1.py"]
    assert run[run.index("-w") + 1] == "/work"


def test_rede_bridge(tmp_path, monkeypatch):
    falso = DockerFalso()
    monkeypatch.setattr(modulo.subprocess, "run", falso)
    Sandbox().executar(tmp_path, "x", rede=True, semente=1)
    run = falso.run_cmd()
    assert run[run.index("--network") + 1] == "bridge"


def test_timeout_mata_container_pelo_nome(tmp_path, monkeypatch):
    falso = DockerFalso(expirar=True)
    monkeypatch.setattr(modulo.subprocess, "run", falso)

    r = Sandbox().executar(tmp_path, "x", rede=False, timeout_s=1, semente=1)

    run = falso.run_cmd()
    nome = run[run.index("--name") + 1]
    assert ["docker", "kill", nome] in falso.comandos
    assert r.codigo_saida == 124 and "parcial" in r.stdout and "timeout" in r.stderr.lower()


def test_numero_do_script_incrementa_por_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(modulo.subprocess, "run", DockerFalso())
    sb = Sandbox()
    a = sb.executar(tmp_path / "a", "1", rede=False, semente=1)
    b = sb.executar(tmp_path / "a", "2", rede=False, semente=1)
    c = sb.executar(tmp_path / "b", "3", rede=False, semente=1)
    assert [a.script.name, b.script.name, c.script.name] == [
        "_script_1.py",
        "_script_2.py",
        "_script_1.py",
    ]


def test_arquivos_novos_compara_mtime_e_tamanho(tmp_path, monkeypatch):
    (tmp_path / "igual.txt").write_text("a")
    (tmp_path / "muda.txt").write_text("a")

    def escrever(_cmd):
        (tmp_path / "muda.txt").write_text("a mais")
        (tmp_path / "novo.txt").write_text("n")

    monkeypatch.setattr(modulo.subprocess, "run", DockerFalso(ao_rodar=escrever))
    r = Sandbox().executar(tmp_path, "x", rede=False, semente=1)
    assert sorted(p.name for p in r.arquivos_novos) == ["muda.txt", "novo.txt"]


def test_docker_indisponivel_levanta_runtime_error(tmp_path, monkeypatch):
    def sem_docker(cmd, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(modulo.subprocess, "run", sem_docker)
    with pytest.raises(RuntimeError, match="Docker"):
        Sandbox().executar(tmp_path, "x", rede=False, semente=1)


def test_imagem_ausente_levanta_runtime_error_com_instrucao(tmp_path, monkeypatch):
    def sem_imagem(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "No such image")

    monkeypatch.setattr(modulo.subprocess, "run", sem_imagem)
    with pytest.raises(RuntimeError, match="docker build"):
        Sandbox().executar(tmp_path, "x", rede=False, semente=1)


def test_semente_fora_do_intervalo_do_python(tmp_path, monkeypatch):
    monkeypatch.setattr(modulo.subprocess, "run", DockerFalso())
    with pytest.raises(ValueError):
        Sandbox().executar(tmp_path, "x", rede=False, semente=-1)
