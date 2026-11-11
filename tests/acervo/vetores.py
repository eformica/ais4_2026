"""Vetores de 1024 dimensões para testes: componentes explícitas, resto zero."""

from ais4.acervo.similaridade import EmbedderFalso

DIMENSAO = 1024


def vetor(*componentes: float) -> list[float]:
    return [*componentes, *([0.0] * (DIMENSAO - len(componentes)))]


class EmbedderInstrumentado(EmbedderFalso):
    """Registra cada chamada a `embed` e falha se algum texto contém um dos `venenos`."""

    def __init__(self, vetores=None, venenos: tuple[str, ...] = ()):
        super().__init__(vetores)
        self.venenos = venenos
        self.chamadas: list[list[str]] = []

    def embed(self, textos):
        self.chamadas.append(list(textos))
        if any(v in t for v in self.venenos for t in textos):
            raise RuntimeError("provedor fora do ar")
        return super().embed(textos)
