"""Registro em memória das classes de playbook, indexadas por (id, versão)."""

from collections.abc import Iterator
from contextlib import contextmanager

from ais4.comum.erros import NaoEncontrado
from ais4.playbooks.base import Playbook

_playbooks: dict[tuple[str, str], Playbook] = {}


def registrar[T: type[Playbook]](cls: T) -> T:
    """Decorador de classe: instancia o playbook e o registra."""
    pb = cls()
    chave = (pb.id, pb.versao)
    if chave in _playbooks:
        raise ValueError(f"Playbook {pb.id} {pb.versao} já registrado")
    _playbooks[chave] = pb
    return cls


def obter(codigo: str, versao: str) -> Playbook:
    try:
        return _playbooks[(codigo, versao)]
    except KeyError:
        raise NaoEncontrado(f"Playbook {codigo} {versao} não registrado") from None


def todos() -> list[Playbook]:
    return list(_playbooks.values())


@contextmanager
def isolado(*, vazio: bool = False) -> Iterator[None]:
    """Restaura o registro ao sair (para testes). `vazio=True` começa sem nada registrado."""
    copia = dict(_playbooks)
    if vazio:
        _playbooks.clear()
    try:
        yield
    finally:
        _playbooks.clear()
        _playbooks.update(copia)
