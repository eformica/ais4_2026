"""Contrato da especificação de projeto (B.3).

`EspecProjeto.model_dump(mode="json")` produz exatamente as 9 chaves exigidas pelo
CHECK de `projetos.especificacao`. Os modelos aceitam listas vazias; os critérios de
aceite de 4.1 são verificados à parte (`validar_espec`), que devolve lacunas.
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Nota = Annotated[int, Field(ge=1, le=5)]


class DefinicaoProblema(BaseModel):
    contexto: str = ""
    pergunta_norteadora: str = ""
    hipoteses: list[str] = []


class Escopo(BaseModel):
    dentro: list[str] = []
    fora: list[str] = []
    recorte_temporal: str = ""
    recorte_geografico: str = ""
    recorte_tematico: str = ""


class Objetivo(BaseModel):
    codigo: str
    descricao: str
    entregavel: str


class Restricao(BaseModel):
    tipo: Literal["prazo", "orcamento", "licenca", "etica", "lgpd", "outro"]
    descricao: str
    metrica: str = ""


class Adaptacao(BaseModel):
    descricao: str
    justificativa: str


class Metodologia(BaseModel):
    playbook_codigo: str = ""
    playbook_versao: str = ""
    adaptacoes: list[Adaptacao] = []


class Fonte(BaseModel):
    nome: str
    url: str | None = None
    cobertura: str = ""
    periodicidade: str = ""
    licenca: str = ""
    status_acesso: Literal["nao_testado", "acessivel", "inacessivel", "nao_localizada"] = (
        "nao_testado"
    )


class ResultadoEsperado(BaseModel):
    tipo: str
    formato: str = ""
    destino: str = ""
    metricas_qualidade: list[str] = []


class Risco(BaseModel):
    categoria: Literal["dados", "metodo", "prazo", "outro"]
    descricao: str
    probabilidade: Nota
    impacto: Nota
    mitigacao: str = ""


class MarcoPlanejado(BaseModel):
    codigo: str
    nome: str
    etapa_codigo: str | None = None
    criterio_saida: str
    exige_gate: bool = False
    papel_revisor: str | None = None
    # "agente:<papel>" ou "humano:<papel>"
    responsavel: Annotated[str, Field(pattern=r"^(agente|humano):.+$")]
    data_planejada: date | None = None


class EspecProjeto(BaseModel):
    definicao_problema: DefinicaoProblema = DefinicaoProblema()
    escopo: Escopo = Escopo()
    objetivos: list[Objetivo] = []
    restricoes: list[Restricao] = []
    metodologia: Metodologia = Metodologia()
    fontes_dados: list[Fonte] = []
    resultados_esperados: list[ResultadoEsperado] = []
    riscos: list[Risco] = []
    cronograma: list[MarcoPlanejado] = []
