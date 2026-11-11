from typing import TYPE_CHECKING

from ais4.comum.erros import RegraViolada

if TYPE_CHECKING:
    from ais4.playbooks.base import Etapa


def validar_dag(etapas: "list[Etapa]") -> None:
    """Levanta `RegraViolada` com o ciclo encontrado em `depende_de`.

    Dependências para etapas inexistentes são ignoradas aqui: quem as detecta é
    `playbooks.dependencias_invalidas`. Só `codigo` e `depende_de` são lidos.
    """
    grafo = {e.codigo: tuple(e.depende_de) for e in etapas}
    estado: dict[str, int] = {}  # ausente = não visitada, 1 = na pilha, 2 = concluída
    caminho: list[str] = []

    def visitar(codigo: str) -> None:
        estado[codigo] = 1
        caminho.append(codigo)
        for dep in grafo[codigo]:
            if dep not in grafo:
                continue
            if estado.get(dep) == 1:
                ciclo = [*caminho[caminho.index(dep) :], dep]
                raise RegraViolada("Ciclo nas dependências das etapas: " + " -> ".join(ciclo))
            if dep not in estado:
                visitar(dep)
        caminho.pop()
        estado[codigo] = 2

    for codigo in grafo:
        if codigo not in estado:
            visitar(codigo)
