"""Critérios de aceite de 4.1 checáveis sem LLM.

`validar_espec` devolve lacunas em vez de levantar exceção: o resultado alimenta a autocrítica
(o redator recebe o texto de cada lacuna como feedback). Os critérios semânticos (pergunta
respondível com os dados, mitigação plausível) ficam com o crítico LLM.
"""

from pydantic import BaseModel

from ais4.comum.espec import EspecProjeto
from ais4.playbooks.base import Etapa

MIN_OBJETIVOS = 3
MAX_OBJETIVOS = 7


class Lacuna(BaseModel):
    """Um critério de aceite não cumprido, em linguagem que o redator consiga corrigir."""

    secao: str
    mensagem: str


class ResultadoCritica(BaseModel):
    """Saída estruturada do `critico_projeto` (a API exige um objeto na raiz, não uma lista)."""

    lacunas: list[Lacuna]


def _vazio(texto: str | None) -> bool:
    return not (texto or "").strip()


def validar_espec(espec: EspecProjeto, etapas: list[Etapa]) -> list[Lacuna]:
    lacunas: list[Lacuna] = []

    def falta(secao: str, mensagem: str) -> None:
        lacunas.append(Lacuna(secao=secao, mensagem=mensagem))

    n = len(espec.objetivos)
    if not MIN_OBJETIVOS <= n <= MAX_OBJETIVOS:
        falta(
            "objetivos",
            f"Há {n} objetivos; o projeto precisa de {MIN_OBJETIVOS} a {MAX_OBJETIVOS}.",
        )
    for o in espec.objetivos:
        if _vazio(o.entregavel):
            falta("objetivos", f"O objetivo {o.codigo} não tem entregável verificável.")

    if not any(not _vazio(item) for item in espec.escopo.fora):
        falta("escopo", "Falta ao menos um item explícito em 'fora do escopo'.")

    for r in espec.restricoes:
        if _vazio(r.metrica):
            falta("restricoes", f"A restrição '{r.descricao}' ({r.tipo}) não tem métrica.")

    for a in espec.metodologia.adaptacoes:
        if _vazio(a.justificativa):
            falta("metodologia", f"A adaptação '{a.descricao}' não tem justificativa.")

    if not any(f.status_acesso == "acessivel" for f in espec.fontes_dados):
        falta("fontes_dados", "Nenhuma fonte de dados com acesso testado (status 'acessivel').")

    for r in espec.resultados_esperados:
        ausentes = [c for c, v in (("formato", r.formato), ("destino", r.destino)) if _vazio(v)]
        if ausentes:
            falta(
                "resultados_esperados",
                f"O resultado '{r.tipo}' não define {' nem '.join(ausentes)}.",
            )

    cobertas = {r.categoria for r in espec.riscos}
    for categoria in ("dados", "metodo"):
        if categoria not in cobertas:
            falta("riscos", f"Nenhum risco da categoria '{categoria}' foi coberto.")

    for m in espec.cronograma:
        if _vazio(m.responsavel):
            falta("cronograma", f"O marco {m.codigo} não tem responsável.")

    for etapa in etapas:
        if not etapa.exige_gate:
            continue
        if not any(
            m.etapa_codigo == etapa.codigo
            and m.exige_gate
            and m.papel_revisor == etapa.papel_revisor
            for m in espec.cronograma
        ):
            falta(
                "cronograma",
                f"A etapa '{etapa.codigo}' exige gate do papel '{etapa.papel_revisor}', mas nenhum "
                "marco do cronograma a cobre com esse gate.",
            )
    return lacunas
