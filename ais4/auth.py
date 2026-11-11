"""Autenticação mínima por token (D12): `config/usuarios.toml` guarda o SHA-256 do token."""

import hashlib
import hmac
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from ais4.comum.erros import Proibido
from ais4.config import obter_config

PAPEIS = frozenset(
    {
        "curador_do_acervo",
        "dono_do_projeto",
        "especialista_de_dominio",
        "revisor_metodologico",
        "responsavel_por_dados",
        "guardiao_de_playbooks",
        "responsavel_tecnico",
        "sistema",
    }
)


@dataclass(frozen=True)
class Usuario:
    nome: str
    papeis: frozenset[str]


@lru_cache
def carregar_usuarios(caminho: str) -> dict[str, Usuario]:
    """Lê o arquivo de usuários e devolve `{token_sha256: Usuario}`.

    Falha cedo, com mensagem clara, se o arquivo faltar ou tiver papel desconhecido.
    """
    arquivo = Path(caminho)
    if not arquivo.is_file():
        raise RuntimeError(
            f"Arquivo de usuários não encontrado: {arquivo}. "
            "Copie config/usuarios.exemplo.toml para config/usuarios.toml."
        )
    with arquivo.open("rb") as f:
        dados = tomllib.load(f)

    usuarios: dict[str, Usuario] = {}
    for item in dados.get("usuario", []):
        papeis = frozenset(item.get("papeis", []))
        desconhecidos = papeis - PAPEIS
        if desconhecidos:
            raise ValueError(
                f"Usuário {item.get('nome')!r} em {arquivo} tem papéis desconhecidos: "
                f"{sorted(desconhecidos)}. Válidos: {sorted(PAPEIS)}."
            )
        usuarios[str(item["token_sha256"]).lower()] = Usuario(nome=item["nome"], papeis=papeis)
    return usuarios


def _nao_autenticado(motivo: str) -> HTTPException:
    return HTTPException(401, motivo, headers={"WWW-Authenticate": "Bearer"})


def usuario_atual(authorization: str | None = Header(default=None)) -> Usuario:
    """Dependência: resolve o usuário a partir de `Authorization: Bearer <token>`."""
    if not authorization:
        raise _nao_autenticado("Token ausente")
    esquema, _, token = authorization.partition(" ")
    if esquema.lower() != "bearer" or not token.strip():
        raise _nao_autenticado("Use o cabeçalho Authorization: Bearer <token>")

    hash_token = hashlib.sha256(token.strip().encode()).hexdigest()
    encontrado: Usuario | None = None
    for hash_usuario, usuario in carregar_usuarios(obter_config().usuarios_arquivo).items():
        if hmac.compare_digest(hash_usuario, hash_token):
            encontrado = usuario
    if encontrado is None:
        raise _nao_autenticado("Token inválido")
    return encontrado


def exigir_papel(papel: str) -> Callable[[Usuario], Usuario]:
    """Dependência: exige que o usuário autenticado tenha `papel`."""

    def dependencia(usuario: Annotated[Usuario, Depends(usuario_atual)]) -> Usuario:
        if papel not in usuario.papeis:
            raise Proibido(f"Requer o papel {papel!r}")
        return usuario

    return dependencia
