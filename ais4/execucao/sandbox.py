"""Sandbox Docker local para o código dos agentes (D10, spec seção 7).

Imagem fixada (`sandbox/Dockerfile`); cada execução registra o digest da imagem e a semente.
"""

import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

IMAGEM_PADRAO = "ais4-sandbox:0.1.0"
LIMITE_SAIDA = 20000
CODIGO_TIMEOUT = 124
PASTA_SCRIPTS = "_scripts"
SEMENTE_MAXIMA = 2**32 - 1  # limite de PYTHONHASHSEED


@dataclass(frozen=True)
class ResultadoExec:
    """`arquivos_novos`: caminhos relativos ao workspace (novos ou modificados, sem `_scripts/`)."""

    stdout: str
    stderr: str
    codigo_saida: int
    arquivos_novos: list[Path]
    ambiente: dict
    script: Path | None = field(default=None)  # o script gravado em `workspace/_scripts/`


def _cauda(texto: str | bytes | None) -> str:
    if texto is None:
        return ""
    if isinstance(texto, bytes):
        texto = texto.decode("utf-8", errors="replace")
    return texto[-LIMITE_SAIDA:]


def _instantaneo(workspace: Path) -> dict[Path, tuple[int, int]]:
    """Caminho relativo -> (mtime_ns, tamanho), ignorando `_scripts/`."""
    resultado: dict[Path, tuple[int, int]] = {}
    for arquivo in workspace.rglob("*"):
        relativo = arquivo.relative_to(workspace)
        if relativo.parts[0] == PASTA_SCRIPTS or not arquivo.is_file():
            continue
        estado = arquivo.stat()
        resultado[relativo] = (estado.st_mtime_ns, estado.st_size)
    return resultado


def _proximo_numero(pasta: Path) -> int:
    numeros = [
        int(p.stem.removeprefix("_script_"))
        for p in pasta.glob("_script_*.py")
        if p.stem.removeprefix("_script_").isdigit()
    ]
    return max(numeros, default=0) + 1


class Sandbox:
    def __init__(self, imagem: str = IMAGEM_PADRAO) -> None:
        self.imagem = imagem
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
        pasta = workspace / PASTA_SCRIPTS
        pasta.mkdir(parents=True, exist_ok=True)
        caminho = pasta / f"_script_{_proximo_numero(pasta)}.py"
        caminho.write_text(script, encoding="utf-8")

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
            "4g",
            "--cpus",
            "2",
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
        except subprocess.TimeoutExpired as erro:
            subprocess.run(["docker", "kill", nome], capture_output=True, check=False, timeout=60)
            stdout = _cauda(erro.stdout)
            stderr = _cauda(_cauda(erro.stderr) + f"\n[timeout após {timeout_s}s]")
            codigo = CODIGO_TIMEOUT

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
            },
            script=caminho,
        )
