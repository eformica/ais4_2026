"""Armazém de artefatos endereçado por conteúdo: `<raiz>/<sha[:2]>/<sha>`, URI `file://`.

A interface (`salvar`/`abrir`/`verificar`) é a que um armazém `s3://` implementaria depois.
"""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ais4.config import obter_config

TAMANHO_BLOCO = 1024 * 1024


class Armazem:
    def __init__(self, raiz: Path | None = None) -> None:
        self.raiz = Path(raiz if raiz is not None else obter_config().artefatos_dir).resolve()

    def salvar(self, dados: bytes | Path) -> tuple[str, str]:
        """Grava o conteúdo e devolve `(uri, sha256)`. Arquivos são lidos em blocos."""
        self.raiz.mkdir(parents=True, exist_ok=True)
        descritor, temporario = tempfile.mkstemp(dir=self.raiz, prefix=".tmp-")
        resumo = hashlib.sha256()
        try:
            with os.fdopen(descritor, "wb") as saida:
                if isinstance(dados, bytes):
                    resumo.update(dados)
                    saida.write(dados)
                else:
                    with Path(dados).open("rb") as entrada:
                        while bloco := entrada.read(TAMANHO_BLOCO):
                            resumo.update(bloco)
                            saida.write(bloco)
            sha = resumo.hexdigest()
            destino = self.raiz / sha[:2] / sha
            if destino.exists():  # mesmo conteúdo já armazenado
                os.unlink(temporario)
            else:
                destino.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporario, destino)
        except BaseException:
            Path(temporario).unlink(missing_ok=True)
            raise
        return destino.as_uri(), sha

    def _caminho(self, uri: str) -> Path:
        partes = urlparse(uri)
        if partes.scheme != "file":
            raise ValueError(f"URI fora do armazém (esquema não suportado): {uri}")
        caminho = Path(url2pathname(unquote(partes.path))).resolve()
        if not caminho.is_relative_to(self.raiz) or caminho == self.raiz:
            raise ValueError(f"URI fora do armazém: {uri}")
        return caminho

    def abrir(self, uri: str) -> BinaryIO:
        """Abre o artefato para leitura. `ValueError` se a URI não aponta para dentro da raiz."""
        return self._caminho(uri).open("rb")

    def verificar(self, uri: str, sha256: str) -> bool:
        """Recalcula o SHA-256 do conteúdo; `False` se divergir ou o arquivo sumiu."""
        try:
            arquivo = self.abrir(uri)
        except FileNotFoundError:
            return False
        resumo = hashlib.sha256()
        with arquivo:
            while bloco := arquivo.read(TAMANHO_BLOCO):
                resumo.update(bloco)
        return resumo.hexdigest() == sha256
