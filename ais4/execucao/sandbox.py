"""Sandbox Docker local para o código dos agentes (D10, spec seção 7).

Imagem fixada (`sandbox/Dockerfile`); cada execução registra o digest da imagem, a semente e as
restrições do container. O workspace é montado em leitura e escrita, então o código do agente
pode plantar links nele: toda escrita do host no workspace passa por `verificar_destino` /
`criar_exclusivo`, que recusam links, junctions e qualquer caminho que escape do workspace.
"""

import os
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

IMAGEM_PADRAO = "ais4-sandbox:0.1.0"
USUARIO_PADRAO = "1000:1000"
LIMITE_SAIDA = 20000
CODIGO_TIMEOUT = 124
PASTA_SCRIPTS = "_scripts"
SEMENTE_MAXIMA = 2**32 - 1  # limite de PYTHONHASHSEED
MEMORIA = "4g"
CPUS = "2"
LIMITE_PROCESSOS = 512
TIMEOUT_KILL_S = 30


class ErroWorkspace(ValueError):
    """O workspace contém um link (ou caminho) que levaria uma escrita do host para fora dele."""


@dataclass(frozen=True)
class ResultadoExec:
    """`arquivos_novos`: caminhos relativos ao workspace (novos ou modificados, sem `_scripts/`)."""

    stdout: str
    stderr: str
    codigo_saida: int
    arquivos_novos: list[Path]
    ambiente: dict
    script: Path | None = field(default=None)  # o script gravado em `workspace/_scripts/`


def e_ligacao(caminho: Path | str) -> bool:
    """Link simbólico, junction ou qualquer ponto de reanálise do Windows (inclui os links
    que um container Linux cria em bind mounts do Docker Desktop)."""
    try:
        estado = os.lstat(caminho)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(estado.st_mode):
        return True
    return bool(getattr(estado, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def verificar_destino(workspace: Path, alvo: Path) -> Path:
    """Garante que `alvo` está dentro de `workspace` e que nenhum componente entre os dois (nem o
    próprio alvo, se já existir) é link. Devolve o caminho absoluto; `ErroWorkspace` se não."""
    raiz = Path(os.path.abspath(workspace))
    absoluto = Path(os.path.abspath(alvo))
    if not absoluto.is_relative_to(raiz):
        raise ErroWorkspace(f"caminho fora do workspace: {alvo}")
    atual = raiz
    for parte in absoluto.relative_to(raiz).parts:
        atual = atual / parte
        if e_ligacao(atual):
            raise ErroWorkspace(
                f"{atual.relative_to(raiz).as_posix()} é um link dentro do workspace; "
                "a escrita foi recusada"
            )
    if not Path(os.path.realpath(absoluto)).is_relative_to(Path(os.path.realpath(raiz))):
        raise ErroWorkspace(f"caminho resolve para fora do workspace: {alvo}")
    return absoluto


def garantir_pasta(workspace: Path, pasta: Path) -> Path:
    """Cria `pasta` (e pais) dentro do workspace, recusando links antes e depois."""
    verificar_destino(workspace, pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    return verificar_destino(workspace, pasta)


def criar_exclusivo(workspace: Path, alvo: Path) -> BinaryIO:
    """Abre `alvo` (novo arquivo) para escrita binária, sem seguir links e sem sobrescrever."""
    absoluto = verificar_destino(workspace, alvo)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(absoluto, flags, 0o644), "wb")


def _cauda(texto: str | bytes | None) -> str:
    if texto is None:
        return ""
    if isinstance(texto, bytes):
        texto = texto.decode("utf-8", errors="replace")
    return texto[-LIMITE_SAIDA:]


def _instantaneo(workspace: Path) -> dict[Path, tuple[int, int]]:
    """Caminho relativo -> (mtime_ns, tamanho) dos arquivos comuns, sem `_scripts/` e sem links."""
    resultado: dict[Path, tuple[int, int]] = {}
    for pasta, subpastas, nomes in os.walk(workspace, followlinks=False):
        pasta = Path(pasta)
        if pasta == workspace:
            subpastas[:] = [s for s in subpastas if s != PASTA_SCRIPTS]
        for nome in nomes:
            arquivo = pasta / nome
            if e_ligacao(arquivo):
                continue
            estado = os.lstat(arquivo)
            resultado[arquivo.relative_to(workspace)] = (estado.st_mtime_ns, estado.st_size)
    return resultado


def _proximo_numero(pasta: Path) -> int:
    numeros = [
        int(p.stem.removeprefix("_script_"))
        for p in pasta.glob("_script_*.py")
        if p.stem.removeprefix("_script_").isdigit()
    ]
    return max(numeros, default=0) + 1


class Sandbox:
    def __init__(self, imagem: str = IMAGEM_PADRAO, usuario: str | None = USUARIO_PADRAO) -> None:
        """`usuario` ("uid:gid") roda o container sem root; `None` omite `--user`."""
        self.imagem = imagem
        self.usuario = usuario
        self._digest: str | None = None

    def _digest_da_imagem(self) -> str:
        if self._digest is None:
            try:
                r = subprocess.run(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", self.imagem],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as erro:
                raise RuntimeError(f"Docker indisponível: {erro}") from erro
            if r.returncode != 0:
                raise RuntimeError(
                    f"Docker indisponível ou imagem {self.imagem} ausente "
                    f"({r.stderr.strip()}); construa com: docker build -t {self.imagem} sandbox/"
                )
            self._digest = r.stdout.strip()
        return self._digest

    def _seguranca(self) -> dict:
        return {
            "cap_drop": "ALL",
            "no_new_privileges": True,
            "pids_limit": LIMITE_PROCESSOS,
            "memoria": MEMORIA,
            "memoria_swap": MEMORIA,
            "cpus": CPUS,
            "usuario": self.usuario,
        }

    def _gravar_script(self, workspace: Path, script: str) -> Path:
        """Grava `workspace/_scripts/_script_<n>.py` sem seguir links. `ErroWorkspace` se houver."""
        try:
            pasta = garantir_pasta(workspace, workspace / PASTA_SCRIPTS)
            caminho = pasta / f"_script_{_proximo_numero(pasta)}.py"
            with criar_exclusivo(workspace, caminho) as arquivo:
                arquivo.write(script.encode("utf-8"))
        except OSError as erro:  # inclui FileExistsError e pasta que virou arquivo
            raise ErroWorkspace(f"workspace inutilizável para gravar o script: {erro}") from erro
        return caminho

    def _matar(self, nome: str) -> None:
        try:
            subprocess.run(
                ["docker", "kill", nome], capture_output=True, check=False, timeout=TIMEOUT_KILL_S
            )
        except (OSError, subprocess.SubprocessError):
            pass  # o container pode já ter terminado (--rm); nada a fazer

    def executar(
        self,
        workspace: Path,
        script: str,
        *,
        rede: bool,
        timeout_s: int = 900,
        semente: int,
    ) -> ResultadoExec:
        if not 0 <= semente <= SEMENTE_MAXIMA:
            raise ValueError(f"semente fora de 0..{SEMENTE_MAXIMA}: {semente}")
        digest = self._digest_da_imagem()
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        caminho = self._gravar_script(workspace, script)

        antes = _instantaneo(workspace)
        nome = f"ais4-sandbox-{uuid.uuid4().hex[:12]}"
        comando = [
            "docker",
            "run",
            "--rm",
            "--name",
            nome,
            "--network",
            "bridge" if rede else "none",
            "--memory",
            MEMORIA,
            "--memory-swap",
            MEMORIA,
            "--cpus",
            CPUS,
            "--pids-limit",
            str(LIMITE_PROCESSOS),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *(["--user", self.usuario] if self.usuario else []),
            "-v",
            f"{workspace.resolve()}:/work",
            "-w",
            "/work",
            "-e",
            f"PYTHONHASHSEED={semente}",
            "-e",
            f"AIS4_SEMENTE={semente}",
            self.imagem,
            "python",
            f"/work/{PASTA_SCRIPTS}/{caminho.name}",
        ]
        terminou = False
        try:
            try:
                r = subprocess.run(
                    comando,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_s,
                    check=False,
                )
                stdout, stderr, codigo = _cauda(r.stdout), _cauda(r.stderr), r.returncode
                terminou = True
            except subprocess.TimeoutExpired as erro:
                stdout = _cauda(erro.stdout)
                stderr = _cauda(_cauda(erro.stderr) + f"\n[timeout após {timeout_s}s]")
                codigo = CODIGO_TIMEOUT
        finally:
            if not terminou:  # timeout ou qualquer exceção: o container não pode sobrar vivo
                self._matar(nome)

        depois = _instantaneo(workspace)
        novos = sorted(p for p, estado in depois.items() if antes.get(p) != estado)
        return ResultadoExec(
            stdout=stdout,
            stderr=stderr,
            codigo_saida=codigo,
            arquivos_novos=novos,
            ambiente={
                "imagem": self.imagem,
                "imagem_digest": digest,
                "semente": semente,
                "rede": rede,
                "seguranca": self._seguranca(),
            },
            script=caminho,
        )
