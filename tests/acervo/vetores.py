"""Vetores de 1024 dimensões para testes: componentes explícitas, resto zero."""

DIMENSAO = 1024


def vetor(*componentes: float) -> list[float]:
    return [*componentes, *([0.0] * (DIMENSAO - len(componentes)))]
