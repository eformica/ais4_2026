"""Serviço do módulo acervo: registro de ideias, ciclo de vida, fila priorizada e histórico.

As transições válidas de status são decididas pelo trigger do banco (`acervo.trg_ideia_transicao`);
aqui só se traduz o erro (RegraViolada) e se registra o que o trigger não cobre (relação de
duplicidade ao fundir).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ais4.acervo.modelos import (
    EventoModelo,
    IdeiaModelo,
    LacunaModelo,
    RelacaoModelo,
    RevisaoDuplicidadeModelo,
)
from ais4.auth import Usuario
from ais4.comum.dto import EventoDTO, IdeiaDTO, LacunaDTO
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.comum.espec import Nota
from ais4.config import obter_config
from ais4.db import sessao
from ais4.playbooks import registro
from ais4.playbooks import servico as playbooks_servico

if TYPE_CHECKING:  # similaridade importa este módulo; o import real é feito em `qualificar`
    from ais4.acervo.similaridade import Embedder

USUARIO_SISTEMA = "sistema"  # autor das leituras (só precisa de um nome para app.usuario)
PAPEL_CURADOR = "curador_do_acervo"
MIN_PALAVRAS_PERGUNTA = 8
LIMITE_CANDIDATAS = 20  # quantas ideias parecidas a qualificação examina

Status = Literal[
    "rascunho", "triagem", "qualificada", "em_projeto", "concluida", "arquivada", "fundida"
]
Tipo = Literal["problema", "oportunidade", "hipotese", "pergunta_de_pesquisa"]
Origem = Literal["humana", "retroalimentacao", "fonte_externa"]
TipoRelacao = Literal["duplicada", "filha", "dependente", "relacionada"]

Titulo = Annotated[str, StringConstraints(min_length=1, max_length=100)]
Dominio = Annotated[list[str], Field(min_length=1)]
PrioridadeManual = Annotated[Decimal, Field(ge=0, max_digits=5, decimal_places=2)]


class IdeiaCriar(BaseModel):
    """Campos de 3.1 que quem registra a ideia informa. `status` inicial: rascunho ou triagem."""

    titulo: Titulo
    tipo: Tipo
    descricao: str
    pergunta_norteadora: str
    dominio: Dominio
    origem: Origem = "humana"
    origem_url: str | None = None
    playbook_sugerido_id: UUID | None = None
    fontes_candidatas: list[dict] = []
    restricoes: str | None = None
    valor: Nota
    viabilidade: Nota
    prioridade_manual: PrioridadeManual | None = None
    status: Literal["rascunho", "triagem"] = "rascunho"

    @model_validator(mode="after")
    def _fonte_externa_exige_url(self):
        if self.origem == "fonte_externa" and not self.origem_url:
            raise ValueError("origem_url é obrigatória quando a origem é fonte_externa")
        return self


# Campos do PATCH que aceitam `null` explícito (limpar o valor); nos demais, `null` é erro.
_CAMPOS_ANULAVEIS = frozenset(
    {"origem_url", "playbook_sugerido_id", "restricoes", "prioridade_manual"}
)


class IdeiaAtualizar(BaseModel):
    """PATCH: só os campos informados mudam. Não edita `origem`, `status` nem `criado_por`."""

    titulo: Titulo | None = None
    tipo: Tipo | None = None
    descricao: str | None = None
    pergunta_norteadora: str | None = None
    dominio: Dominio | None = None
    origem_url: str | None = None
    playbook_sugerido_id: UUID | None = None
    fontes_candidatas: list[dict] | None = None
    restricoes: str | None = None
    valor: Nota | None = None
    viabilidade: Nota | None = None
    prioridade_manual: PrioridadeManual | None = None

    @model_validator(mode="after")
    def _null_so_onde_a_coluna_aceita(self):
        for campo in self.model_fields_set - _CAMPOS_ANULAVEIS:
            if getattr(self, campo) is None:
                raise ValueError(f"{campo} não aceita null")
        return self


# ---------------------------------------------------------------------------
# Conversão linha -> DTO e acesso
# ---------------------------------------------------------------------------
def _dto(linha: IdeiaModelo) -> IdeiaDTO:
    return IdeiaDTO(
        id=linha.id,
        titulo=linha.titulo,
        tipo=linha.tipo,
        descricao=linha.descricao,
        pergunta_norteadora=linha.pergunta_norteadora,
        dominio=list(linha.dominio),
        origem=linha.origem,
        origem_url=linha.origem_url,
        playbook_sugerido_id=linha.playbook_sugerido_id,
        fontes_candidatas=list(linha.fontes_candidatas),
        restricoes=linha.restricoes,
        valor=linha.valor,
        viabilidade=linha.viabilidade,
        prioridade_manual=linha.prioridade_manual,
        prioridade=linha.prioridade,
        status=linha.status,
        motivo_status=linha.motivo_status,
        fundida_em_id=linha.fundida_em_id,
        criado_por=linha.criado_por,
        criado_em=linha.criado_em,
        atualizado_em=linha.atualizado_em,
    )


def _linha(s: Session, id: UUID, *, bloquear: bool = False) -> IdeiaModelo:
    consulta = select(IdeiaModelo).where(IdeiaModelo.id == id, IdeiaModelo.excluido_em.is_(None))
    if bloquear:
        consulta = consulta.with_for_update()
    linha = s.scalars(consulta).first()
    if linha is None:
        raise NaoEncontrado(f"Ideia {id} não encontrada")
    return linha


def _gravar(s: Session, linha: IdeiaModelo) -> IdeiaDTO:
    """Envia as mudanças (o banco valida) e relê colunas geradas pelo banco."""
    s.flush()
    s.refresh(linha)
    return _dto(linha)


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------
def criar_ideia(dados: IdeiaCriar, usuario: str) -> IdeiaDTO:
    with sessao("acervo", usuario) as s:
        linha = IdeiaModelo(**dados.model_dump())
        s.add(linha)
        return _gravar(s, linha)


def criar_ideia_derivada(id: UUID, dados: IdeiaCriar, usuario: str) -> IdeiaDTO:
    """Cria a ideia de retroalimentação com id determinístico; repetir a chamada não duplica."""
    valores = dados.model_dump() | {"origem": "retroalimentacao"}
    with sessao("acervo", usuario) as s:
        s.execute(
            pg_insert(IdeiaModelo.__table__)
            .values(id=id, **valores)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        linha = s.get(IdeiaModelo, id)
        if linha is None:  # inatingível: o INSERT acima cria ou já existia
            raise NaoEncontrado(f"Ideia {id} não encontrada")
        return _dto(linha)


def obter(id: UUID) -> IdeiaDTO:
    with sessao("acervo", USUARIO_SISTEMA) as s:
        return _dto(_linha(s, id))


def atualizar(id: UUID, dados: IdeiaAtualizar, usuario: str) -> IdeiaDTO:
    campos = dados.model_dump(exclude_unset=True)
    with sessao("acervo", usuario) as s:
        linha = _linha(s, id, bloquear=True)
        if not campos:
            return _dto(linha)
        for campo, valor in campos.items():
            setattr(linha, campo, valor)
        return _gravar(s, linha)


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------
def transicionar(
    id: UUID,
    para: str,
    usuario: str,
    motivo: str | None = None,
    fundida_em_id: UUID | None = None,
) -> IdeiaDTO:
    """Muda o status; o trigger do banco decide se a transição é válida (senão, RegraViolada).

    `motivo_status` passa a ser o `motivo` informado (ou NULL). Fundir exige `fundida_em_id` e
    registra a relação `duplicada` entre as duas ideias.
    """
    if para == "fundida" and fundida_em_id is None:
        raise RegraViolada("fundida_em_id é obrigatório para fundir uma ideia")
    if para != "fundida" and fundida_em_id is not None:
        raise RegraViolada("fundida_em_id só se aplica à transição para fundida")
    with sessao("acervo", usuario) as s:
        linha = _linha(s, id, bloquear=True)
        if linha.status == para:
            raise RegraViolada(f"A ideia já está no status {para}")
        linha.status = para
        linha.motivo_status = (motivo or "").strip() or None
        linha.fundida_em_id = fundida_em_id
        dto = _gravar(s, linha)
        if para == "fundida":
            _inserir_relacao(s, id, fundida_em_id, "duplicada")
        return dto


def fila(status: str = "qualificada", limite: int = 20) -> list[IdeiaDTO]:
    """Ideias do status pedido, da mais prioritária para a menos; empate: a mais antiga primeiro."""
    with sessao("acervo", USUARIO_SISTEMA) as s:
        linhas = s.scalars(
            select(IdeiaModelo)
            .where(IdeiaModelo.status == status, IdeiaModelo.excluido_em.is_(None))
            .order_by(IdeiaModelo.prioridade.desc(), IdeiaModelo.criado_em.asc())
            .limit(limite)
        )
        return [_dto(linha) for linha in linhas]


# ---------------------------------------------------------------------------
# Relações e histórico
# ---------------------------------------------------------------------------
def _inserir_relacao(s: Session, ideia_id: UUID, relacionada_id: UUID, tipo: str) -> None:
    s.execute(
        pg_insert(RelacaoModelo.__table__)
        .values(ideia_id=ideia_id, relacionada_id=relacionada_id, tipo=tipo)
        .on_conflict_do_nothing()
    )


def relacionar(ideia_id: UUID, relacionada_id: UUID, tipo: str, usuario: str) -> None:
    """Liga duas ideias existentes (idempotente para o mesmo par e tipo)."""
    with sessao("acervo", usuario) as s:
        ids = {ideia_id, relacionada_id}
        existentes = s.scalar(
            select(func.count())
            .select_from(IdeiaModelo)
            .where(IdeiaModelo.id.in_(ids), IdeiaModelo.excluido_em.is_(None))
        )
        if existentes != len(ids):
            raise NaoEncontrado(f"Ideia {ideia_id} ou {relacionada_id} não encontrada")
        _inserir_relacao(s, ideia_id, relacionada_id, tipo)


def historico(id: UUID) -> list[EventoDTO]:
    with sessao("acervo", USUARIO_SISTEMA) as s:
        _linha(s, id)
        eventos = s.scalars(
            select(EventoModelo)
            .where(EventoModelo.ideia_id == id)
            .order_by(EventoModelo.ocorrido_em, EventoModelo.id)
        )
        return [
            EventoDTO(
                id=e.id,
                ideia_id=e.ideia_id,
                evento=e.evento,
                de_status=e.de_status,
                para_status=e.para_status,
                motivo=e.motivo,
                autor=e.autor,
                ocorrido_em=e.ocorrido_em,
            )
            for e in eventos
        ]


# ---------------------------------------------------------------------------
# Lacunas e revisão de duplicidade
# ---------------------------------------------------------------------------
def exigir_curador(usuario: Usuario) -> None:
    if PAPEL_CURADOR not in usuario.papeis:
        raise Proibido(f"Requer o papel {PAPEL_CURADOR!r}")


def registrar_lacuna(ideia_id: UUID, descricao: str, classes: list[str], usuario: Usuario) -> UUID:
    """Registra que nenhum playbook ativo cobre a ideia (status `aberta`). Só o curador."""
    exigir_curador(usuario)
    with sessao("acervo", usuario.nome) as s:
        _linha(s, ideia_id)
        lacuna = LacunaModelo(ideia_id=ideia_id, descricao=descricao, classes_requeridas=classes)
        s.add(lacuna)
        s.flush()
        return lacuna.id


def lacunas(status: str = "aberta") -> list[LacunaDTO]:
    with sessao("acervo", USUARIO_SISTEMA) as s:
        linhas = s.scalars(
            select(LacunaModelo)
            .where(LacunaModelo.status == status)
            .order_by(LacunaModelo.criado_em, LacunaModelo.id)
        )
        return [
            LacunaDTO(
                id=x.id,
                ideia_id=x.ideia_id,
                descricao=x.descricao,
                classes_requeridas=list(x.classes_requeridas),
                status=x.status,
                criado_em=x.criado_em,
                resolvida_em=x.resolvida_em,
            )
            for x in linhas
        ]


def registrar_revisao_duplicidade(
    ideia_id: UUID,
    candidata_id: UUID,
    similaridade: float,
    decisao: Literal["distinta", "duplicada"],
    revisor: str,
) -> None:
    """Grava a decisão humana sobre o par; reavaliar o par substitui a decisão anterior."""
    if decisao not in ("distinta", "duplicada"):
        raise RegraViolada(f"Decisão inválida: {decisao!r} (use 'distinta' ou 'duplicada')")
    if ideia_id == candidata_id:
        raise RegraViolada("Uma ideia não pode ser duplicata de si mesma")
    valores = {
        "ideia_id": ideia_id,
        "candidata_id": candidata_id,
        "similaridade": similaridade,
        "decisao": decisao,
        "revisor": revisor,
    }
    with sessao("acervo", revisor) as s:
        _linha(s, ideia_id)
        _linha(s, candidata_id)
        s.execute(
            pg_insert(RevisaoDuplicidadeModelo.__table__)
            .values(**valores)
            .on_conflict_do_update(
                index_elements=["ideia_id", "candidata_id"],
                set_=valores | {"decidido_em": func.now()},
            )
        )


# ---------------------------------------------------------------------------
# Gate "Qualificação de ideia" (3.3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResultadoQualificacao:
    """`pendencias`: textos curtos que começam com uma etiqueta estável (`pergunta:`, `fonte:`,
    `duplicidade:`, `playbook:`). `candidatas_duplicidade`: ideias com similaridade acima do
    limiar (revisadas ou não), com a similaridade."""

    qualificada: bool
    pendencias: list[str]
    candidatas_duplicidade: list[tuple[UUID, float]]


def _pendencia_pergunta(ideia: IdeiaDTO) -> str | None:
    pergunta = ideia.pergunta_norteadora.strip()
    if not pergunta.endswith("?") or len(pergunta.split()) < MIN_PALAVRAS_PERGUNTA:
        return (
            "pergunta: a pergunta norteadora deve terminar em '?' e ter ao menos "
            f"{MIN_PALAVRAS_PERGUNTA} palavras (não pode ser só um tema)"
        )
    return None


def _pendencias_duplicidade(ideia_id: UUID, candidatas: list[tuple[UUID, float]]) -> list[str]:
    if not candidatas:
        return []
    with sessao("acervo", USUARIO_SISTEMA) as s:
        linhas = s.execute(
            select(RevisaoDuplicidadeModelo.candidata_id, RevisaoDuplicidadeModelo.decisao).where(
                RevisaoDuplicidadeModelo.ideia_id == ideia_id
            )
        )
        decisoes = {candidata_id: decisao for candidata_id, decisao in linhas}
    pendencias = []
    for candidata_id, similaridade in candidatas:
        decisao = decisoes.get(candidata_id)
        if decisao == "distinta":
            continue
        situacao = "revisada como duplicada" if decisao == "duplicada" else "sem revisão"
        pendencias.append(
            f"duplicidade: ideia {candidata_id} com similaridade {similaridade:.2f} ({situacao})"
        )
    return pendencias


def _ha_lacuna_aberta(ideia_id: UUID) -> bool:
    with sessao("acervo", USUARIO_SISTEMA) as s:
        total = s.scalar(
            select(func.count())
            .select_from(LacunaModelo)
            .where(LacunaModelo.ideia_id == ideia_id, LacunaModelo.status == "aberta")
        )
        return total > 0


def _algum_playbook_aplicavel(ideia: IdeiaDTO) -> bool:
    for versao in playbooks_servico.versoes_ativas():
        try:
            playbook = registro.obter(versao.codigo, versao.versao)
        except NaoEncontrado:
            continue
        if playbook.aplicavel(ideia).aplicavel:
            return True
    return False


def qualificar(id: UUID, usuario: Usuario, embedder: "Embedder") -> ResultadoQualificacao:
    """Aplica os critérios de 3.3; sem pendências, move a ideia de `triagem` para `qualificada`.

    Exige o papel curador e que o curador não seja o autor da ideia. Indexa a ideia (e as ainda
    não indexadas, em lote e sem falhar por causa delas) antes, para a similaridade estar atualizada.
    """
    from ais4.acervo import similaridade

    exigir_curador(usuario)
    ideia = obter(id)
    if usuario.nome == ideia.criado_por:
        raise Proibido("O autor da ideia não pode qualificá-la")
    if ideia.status != "triagem":
        raise RegraViolada(f"Só ideias em triagem podem ser qualificadas (status: {ideia.status})")

    pendencias: list[str] = []
    if (pendencia := _pendencia_pergunta(ideia)) is not None:
        pendencias.append(pendencia)
    if not ideia.fontes_candidatas:
        pendencias.append("fonte: informe ao menos uma fonte de dados candidata")

    similaridade.indexar(id, embedder)  # ServicoIndisponivel: nada foi alterado
    similaridade.indexar_pendentes(embedder)
    sem_indice = similaridade.ideias_sem_indice()
    limiar = obter_config().limiar_duplicidade
    candidatas = [
        (outra.id, sim)
        for outra, sim in similaridade.similares(id, limite=LIMITE_CANDIDATAS)
        if sim >= limiar
    ]
    pendencias += _pendencias_duplicidade(id, candidatas)
    if sem_indice:
        ids = ", ".join(str(x) for x in sem_indice[:10]) + ("..." if len(sem_indice) > 10 else "")
        pendencias.append(
            f"duplicidade: {len(sem_indice)} ideia(s) ainda sem índice (ids: {ids}); "
            "a checagem de duplicidade não está completa"
        )

    if not (_algum_playbook_aplicavel(ideia) or _ha_lacuna_aberta(id)):
        pendencias.append(
            "playbook: nenhum playbook ativo é aplicável e não há lacuna aberta registrada"
        )

    if pendencias:
        return ResultadoQualificacao(False, pendencias, candidatas)
    transicionar(id, "qualificada", usuario.nome)
    return ResultadoQualificacao(True, [], candidatas)
