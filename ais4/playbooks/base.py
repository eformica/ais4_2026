"""Classe abstrata `Playbook` (seção 6.1) e os value objects que ela usa."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from ais4.comum.dto import ArtefatoRef, IdeiaDTO
from ais4.comum.espec import EspecProjeto


@dataclass(frozen=True)
class Requisito:
    tipo: str  # ex.: dados, ambiente, competencia
    descricao: str


@dataclass(frozen=True)
class PapelAgente:
    papel: str
    # Vive só na classe Python (versionada pelo código + versão semver); o banco guarda
    # apenas {"papel", "ferramentas"}.
    prompt_base: str
    ferramentas: tuple[str, ...] = ()


@dataclass(frozen=True)
class Etapa:
    codigo: str
    ordem: int
    nome: str
    descricao: str | None
    depende_de: tuple[str, ...]
    papel_agente: PapelAgente
    entradas: tuple[str, ...]
    saidas: tuple[str, ...]
    criterio_saida: str
    exige_gate: bool = False
    papel_revisor: str | None = None


@dataclass(frozen=True)
class Criterio:
    codigo: str
    descricao: str


@dataclass(frozen=True)
class Entregavel:
    tipo: str


@dataclass(frozen=True)
class Avaliacao:
    aplicavel: bool
    pontuacao: float
    lacunas: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SaidaEtapa:
    artefatos: dict[str, ArtefatoRef]
    resumo: str


@dataclass(frozen=True)
class Resultado:
    aprovado: bool
    feedback: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IdeiaNova:
    titulo: str
    tipo: str
    descricao: str
    pergunta_norteadora: str
    dominio: list[str]
    valor: int
    viabilidade: int


TITULO_MAX = 100


class Playbook(ABC):
    """Metodologia empacotada como classe. Subclasses preenchem os atributos de classe.

    `id` é o `codigo` do playbook (ex.: "projeto-academico-simples"); o uuid do banco é
    resolvido por `servico.sincronizar`. `nome`, `descricao` e `changelog` completam as
    colunas de `playbooks.playbook` / `playbook_versao` que a spec 6.1 não lista.
    """

    id: ClassVar[str]
    versao: ClassVar[str]  # versionamento semântico: major.minor.patch
    nome: ClassVar[str]
    descricao: ClassVar[str | None] = None
    changelog: ClassVar[str | None] = None
    classe_de_problema: ClassVar[list[str]]
    pre_requisitos: ClassVar[list[Requisito]]
    template_projeto: ClassVar[dict]
    etapas: ClassVar[list[Etapa]]
    criterios_qualidade: ClassVar[list[Criterio]]
    entregaveis: ClassVar[list[Entregavel]]

    @abstractmethod
    def aplicavel(self, ideia: IdeiaDTO) -> Avaliacao:
        """Casa ideia x playbook."""

    @abstractmethod
    def instanciar(self, ideia: IdeiaDTO) -> EspecProjeto:
        """Rascunho de especificação; usado pelo Motor de Criação."""

    @abstractmethod
    def validar_etapa(self, etapa: Etapa, saida: SaidaEtapa) -> Resultado:
        """Checagem de qualidade da saída de uma etapa."""

    def retroalimentar(self, proximos_passos: list[str], ideia_origem: IdeiaDTO) -> list[IdeiaNova]:
        """Padrão: uma ideia por próximo passo, herdando o domínio da ideia de origem."""
        return [
            IdeiaNova(
                titulo=_titulo(passo),
                tipo="pergunta_de_pesquisa",
                descricao=passo,
                pergunta_norteadora=passo,
                dominio=list(ideia_origem.dominio),
                valor=3,
                viabilidade=3,
            )
            for passo in proximos_passos
        ]


def _titulo(passo: str) -> str:
    passo = passo.strip()
    return passo if len(passo) <= TITULO_MAX else passo[: TITULO_MAX - 1] + "…"
