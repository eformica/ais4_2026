"""Serviço do módulo projetos: criação atômica, especificação versionada, marcos e gates.

As regras de transição (projeto e marco) e as exigências de fechamento (lições, próximos passos,
gates aprovados) são decididas pelos triggers do banco; aqui só se traduz o erro (RegraViolada) e
se aplica o que o banco não sabe: quem pode decidir um gate (papel + pessoa designada).
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ais4.auth import PAPEIS, Usuario
from ais4.comum.dto import (
    ArtefatoDTO,
    ArtefatoRef,
    AvaliacaoDTO,
    EspecificacaoDTO,
    LicaoDTO,
    MarcoDTO,
    ProjetoDTO,
    ProximoPassoDTO,
)
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.comum.espec import EspecProjeto, Nota
from ais4.db import sessao
from ais4.projetos.modelos import (
    ArtefatoModelo,
    AvaliacaoModelo,
    EspecificacaoModelo,
    LicaoModelo,
    MarcoModelo,
    ProjetoModelo,
    ProximoPassoModelo,
    ResponsavelModelo,
)

USUARIO_SISTEMA = "sistema"  # autor das leituras e das escritas feitas pelo próprio sistema
PAPEIS_AVALIADORES = frozenset(
    {"dono_do_projeto", "revisor_metodologico", "especialista_de_dominio"}
)
STATUS_MARCO_DO_SISTEMA = frozenset({"em_andamento", "aguardando_gate", "aprovado"})

TipoArtefato = Literal["dataset", "codigo", "grafico", "relatorio", "apresentacao", "outro"]
TipoLicao = Literal["funcionou", "falhou", "sugestao"]


class CriarProjeto(BaseModel):
    """Tudo o que nasce junto com o projeto: especificação v1, marcos do cronograma e responsáveis."""

    titulo: str
    ideia_origem_id: UUID
    playbook_id: UUID
    playbook_versao: str
    dono: str
    especificacao: EspecProjeto
    aprovado_por: str
    responsaveis: dict[str, str] = {}

    @field_validator("responsaveis")
    @classmethod
    def _papeis_conhecidos(cls, valor: dict[str, str]) -> dict[str, str]:
        # papel digitado errado desativaria, em silêncio, a checagem de pessoa designada do gate
        desconhecidos = set(valor) - PAPEIS
        if desconhecidos:
            raise ValueError(f"papéis desconhecidos em responsaveis: {sorted(desconhecidos)}")
        return valor


# ---------------------------------------------------------------------------
# Conversão linha -> DTO e acesso
# ---------------------------------------------------------------------------
def _dto_projeto(p: ProjetoModelo) -> ProjetoDTO:
    return ProjetoDTO(
        id=p.id,
        titulo=p.titulo,
        ideia_origem_id=p.ideia_origem_id,
        playbook_id=p.playbook_id,
        playbook_versao=p.playbook_versao,
        status=p.status,
        motivo_status=p.motivo_status,
        dono=p.dono,
        criado_em=p.criado_em,
        atualizado_em=p.atualizado_em,
    )


def _dto_marco(m: MarcoModelo) -> MarcoDTO:
    return MarcoDTO(
        id=m.id,
        projeto_id=m.projeto_id,
        codigo=m.codigo,
        ordem=m.ordem,
        nome=m.nome,
        etapa_codigo=m.etapa_codigo,
        criterio_saida=m.criterio_saida,
        exige_gate=m.exige_gate,
        papel_revisor=m.papel_revisor,
        revisor=m.revisor,
        status=m.status,
        data_planejada=m.data_planejada,
        data_real=m.data_real,
        decisao_gate=m.decisao_gate,
        decidido_em=m.decidido_em,
    )


def _dto_artefato(a: ArtefatoModelo) -> ArtefatoDTO:
    return ArtefatoDTO(
        id=a.id,
        projeto_id=a.projeto_id,
        marco_id=a.marco_id,
        tipo=a.tipo,
        nome=a.nome,
        uri=a.uri,
        hash_sha256=a.hash_sha256,
        proveniencia=dict(a.proveniencia),
        criado_em=a.criado_em,
    )


def _dto_avaliacao(a: AvaliacaoModelo) -> AvaliacaoDTO:
    return AvaliacaoDTO(
        id=a.id,
        projeto_id=a.projeto_id,
        criterio=a.criterio,
        nota=a.nota,
        comentario=a.comentario,
        revisor=a.revisor,
        avaliado_em=a.avaliado_em,
    )


def _dto_licao(li: LicaoModelo) -> LicaoDTO:
    return LicaoDTO(
        id=li.id,
        projeto_id=li.projeto_id,
        tipo=li.tipo,
        descricao=li.descricao,
        playbook_alvo_id=li.playbook_alvo_id,
        encaminhada_em=li.encaminhada_em,
        criado_em=li.criado_em,
    )


def _dto_passo(p: ProximoPassoModelo) -> ProximoPassoDTO:
    return ProximoPassoDTO(
        id=p.id,
        projeto_id=p.projeto_id,
        descricao=p.descricao,
        ideia_gerada_id=p.ideia_gerada_id,
        descartado_motivo=p.descartado_motivo,
        criado_em=p.criado_em,
    )


def _projeto(s: Session, id: UUID, *, bloquear: bool = False) -> ProjetoModelo:
    consulta = select(ProjetoModelo).where(
        ProjetoModelo.id == id, ProjetoModelo.excluido_em.is_(None)
    )
    if bloquear:
        consulta = consulta.with_for_update()
    linha = s.scalars(consulta).first()
    if linha is None:
        raise NaoEncontrado(f"Projeto {id} não encontrado")
    return linha


def _marco(s: Session, projeto_id: UUID, codigo: str, *, bloquear: bool = False) -> MarcoModelo:
    consulta = select(MarcoModelo).where(
        MarcoModelo.projeto_id == projeto_id, MarcoModelo.codigo == codigo
    )
    if bloquear:
        consulta = consulta.with_for_update()
    linha = s.scalars(consulta).first()
    if linha is None:
        raise NaoEncontrado(f"Marco {codigo} não encontrado no projeto {projeto_id}")
    return linha


def _passo(s: Session, id: UUID) -> ProximoPassoModelo:
    linha = s.scalars(
        select(ProximoPassoModelo).where(ProximoPassoModelo.id == id).with_for_update()
    ).first()
    if linha is None:
        raise NaoEncontrado(f"Próximo passo {id} não encontrado")
    return linha


# ---------------------------------------------------------------------------
# Projeto
# ---------------------------------------------------------------------------
def criar_projeto(cmd: CriarProjeto, usuario: str) -> ProjetoDTO:
    """Projeto, especificação v1, um marco por item do cronograma e responsáveis: tudo ou nada."""
    with sessao("projetos", usuario) as s:
        projeto = ProjetoModelo(
            titulo=cmd.titulo,
            ideia_origem_id=cmd.ideia_origem_id,
            playbook_id=cmd.playbook_id,
            playbook_versao=cmd.playbook_versao,
            dono=cmd.dono,
        )
        s.add(projeto)
        s.flush()  # as demais linhas referenciam o id; o ORM não conhece as FKs

        s.add(
            EspecificacaoModelo(
                projeto_id=projeto.id,
                versao=1,
                conteudo=cmd.especificacao.model_dump(mode="json"),
                aprovado_por=cmd.aprovado_por,
            )
        )
        for ordem, m in enumerate(cmd.especificacao.cronograma, start=1):
            s.add(
                MarcoModelo(
                    projeto_id=projeto.id,
                    codigo=m.codigo,
                    ordem=ordem,
                    nome=m.nome,
                    etapa_codigo=m.etapa_codigo,
                    criterio_saida=m.criterio_saida,
                    exige_gate=m.exige_gate,
                    papel_revisor=m.papel_revisor,
                    data_planejada=m.data_planejada,
                )
            )
        for papel, pessoa in cmd.responsaveis.items():
            s.add(ResponsavelModelo(projeto_id=projeto.id, papel=papel, pessoa=pessoa))
        s.flush()
        s.refresh(projeto)
        return _dto_projeto(projeto)


def obter(projeto_id: UUID) -> ProjetoDTO:
    with sessao("projetos", USUARIO_SISTEMA) as s:
        return _dto_projeto(_projeto(s, projeto_id))


def listar(status: str | None = None, limite: int = 50) -> list[ProjetoDTO]:
    """Projetos do mais recente para o mais antigo, opcionalmente de um só status."""
    consulta = select(ProjetoModelo).where(ProjetoModelo.excluido_em.is_(None))
    if status is not None:
        consulta = consulta.where(ProjetoModelo.status == status)
    with sessao("projetos", USUARIO_SISTEMA) as s:
        linhas = s.scalars(consulta.order_by(ProjetoModelo.criado_em.desc()).limit(limite))
        return [_dto_projeto(p) for p in linhas]


def contar_por_status(status: str) -> int:
    """Quantos projetos (não excluídos) estão no status pedido."""
    with sessao("projetos", USUARIO_SISTEMA) as s:
        return s.scalar(
            select(func.count())
            .select_from(ProjetoModelo)
            .where(ProjetoModelo.status == status, ProjetoModelo.excluido_em.is_(None))
        )


def transicionar(
    projeto_id: UUID, para: str, usuario: str, motivo: str | None = None
) -> ProjetoDTO:
    """Muda o status; o trigger do banco decide se a transição é válida (senão, RegraViolada).

    `motivo_status` passa a ser o `motivo` informado (ou NULL); pausar e cancelar exigem motivo.
    """
    with sessao("projetos", usuario) as s:
        projeto = _projeto(s, projeto_id, bloquear=True)
        if projeto.status == para:
            raise RegraViolada(f"O projeto já está no status {para}")
        projeto.status = para
        projeto.motivo_status = (motivo or "").strip() or None
        s.flush()
        s.refresh(projeto)
        return _dto_projeto(projeto)


# ---------------------------------------------------------------------------
# Especificação (versionada, imutável)
# ---------------------------------------------------------------------------
def nova_versao_especificacao(
    projeto_id: UUID, espec: EspecProjeto, justificativa: str, usuario: str
) -> int:
    """Grava a próxima versão da especificação; mudança de escopo exige justificativa."""
    justificativa = justificativa.strip()
    if not justificativa:
        raise RegraViolada("Nova versão da especificação exige justificativa")
    with sessao("projetos", usuario) as s:
        _projeto(s, projeto_id, bloquear=True)  # serializa versões concorrentes
        atual = s.scalar(
            select(func.max(EspecificacaoModelo.versao)).where(
                EspecificacaoModelo.projeto_id == projeto_id
            )
        )
        versao = (atual or 0) + 1
        s.add(
            EspecificacaoModelo(
                projeto_id=projeto_id,
                versao=versao,
                conteudo=espec.model_dump(mode="json"),
                justificativa=justificativa,
                aprovado_por=usuario,
            )
        )
        s.flush()
        return versao


def obter_especificacao(projeto_id: UUID, versao: int | None = None) -> EspecificacaoDTO:
    """A versão pedida da especificação, ou a mais alta quando `versao` é omitida."""
    with sessao("projetos", USUARIO_SISTEMA) as s:
        _projeto(s, projeto_id)
        consulta = select(EspecificacaoModelo).where(EspecificacaoModelo.projeto_id == projeto_id)
        if versao is None:
            consulta = consulta.order_by(EspecificacaoModelo.versao.desc()).limit(1)
        else:
            consulta = consulta.where(EspecificacaoModelo.versao == versao)
        linha = s.scalars(consulta).first()
        if linha is None:
            raise NaoEncontrado(
                f"Especificação do projeto {projeto_id} não encontrada"
                + (f" (versão {versao})" if versao is not None else "")
            )
        return EspecificacaoDTO(
            projeto_id=linha.projeto_id,
            versao=linha.versao,
            especificacao=EspecProjeto.model_validate(linha.conteudo),
            justificativa=linha.justificativa,
            aprovado_por=linha.aprovado_por,
            aprovado_em=linha.aprovado_em,
        )


def especificacao_vigente(projeto_id: UUID) -> EspecProjeto:
    return obter_especificacao(projeto_id).especificacao


# ---------------------------------------------------------------------------
# Marcos e gates
# ---------------------------------------------------------------------------
def marcos(projeto_id: UUID) -> list[MarcoDTO]:
    with sessao("projetos", USUARIO_SISTEMA) as s:
        _projeto(s, projeto_id)
        linhas = s.scalars(
            select(MarcoModelo)
            .where(MarcoModelo.projeto_id == projeto_id)
            .order_by(MarcoModelo.ordem)
        )
        return [_dto_marco(m) for m in linhas]


def marco_por_etapa(projeto_id: UUID, etapa_codigo: str) -> MarcoDTO | None:
    with sessao("projetos", USUARIO_SISTEMA) as s:
        _projeto(s, projeto_id)
        linha = s.scalars(
            select(MarcoModelo)
            .where(MarcoModelo.projeto_id == projeto_id, MarcoModelo.etapa_codigo == etapa_codigo)
            .order_by(MarcoModelo.ordem)
        ).first()
        return _dto_marco(linha) if linha else None


def atualizar_marco(projeto_id: UUID, codigo: str, status: str, usuario: str) -> MarcoDTO:
    """Uso do sistema: `em_andamento`, `aguardando_gate` e `aprovado` (só em marco sem gate).

    Reprovar ou aprovar um gate é ato humano e passa por `decidir_gate`. A validade da transição
    em si é do trigger do banco.
    """
    if status not in STATUS_MARCO_DO_SISTEMA:
        raise RegraViolada(
            f"O sistema só move marcos para {sorted(STATUS_MARCO_DO_SISTEMA)}; recebido {status!r}"
        )
    with sessao("projetos", usuario) as s:
        marco = _marco(s, projeto_id, codigo, bloquear=True)
        if status == "aprovado" and marco.exige_gate:
            raise RegraViolada(f"O marco {codigo} exige gate: a aprovação é decisão do revisor")
        marco.status = status
        if status == "aprovado":
            marco.data_real = func.current_date()
        s.flush()
        s.refresh(marco)
        return _dto_marco(marco)


def decidir_gate(
    projeto_id: UUID,
    codigo: str,
    decisao: Literal["aprovado", "reprovado"],
    comentario: str,
    usuario: Usuario,
) -> MarcoDTO:
    """Aprova ou reprova um gate.

    Exige o papel `papel_revisor` do marco e, se o projeto designou uma pessoa para esse papel,
    que seja a própria. `comentario` vira `decisao_gate` (o feedback que reabre a etapa).
    """
    comentario = comentario.strip()
    with sessao("projetos", usuario.nome) as s:
        marco = _marco(s, projeto_id, codigo, bloquear=True)
        if not marco.exige_gate:
            raise RegraViolada(f"O marco {codigo} não exige gate")
        if marco.papel_revisor not in usuario.papeis:
            raise Proibido(f"O gate {codigo} exige o papel {marco.papel_revisor!r}")
        designado = s.scalar(
            select(ResponsavelModelo.pessoa).where(
                ResponsavelModelo.projeto_id == projeto_id,
                ResponsavelModelo.papel == marco.papel_revisor,
            )
        )
        if designado is not None and designado != usuario.nome:
            raise Proibido(
                f"O gate {codigo} foi designado a {designado!r} "
                f"({marco.papel_revisor}), não a {usuario.nome!r}"
            )
        if marco.status != "aguardando_gate":
            raise RegraViolada(
                f"O marco {codigo} está {marco.status!r}; só se decide um gate em aguardando_gate"
            )
        if decisao == "reprovado" and not comentario:
            raise RegraViolada("Reprovar um gate exige comentário (é o feedback para refazer)")
        marco.status = decisao
        marco.revisor = usuario.nome
        marco.decisao_gate = comentario or None
        marco.decidido_em = datetime.now(UTC)
        if decisao == "aprovado":
            marco.data_real = func.current_date()
        s.flush()
        s.refresh(marco)
        return _dto_marco(marco)


# ---------------------------------------------------------------------------
# Artefatos
# ---------------------------------------------------------------------------
def registrar_artefato(
    projeto_id: UUID,
    marco_id: UUID | None,
    tipo: TipoArtefato,
    nome: str,
    ref: ArtefatoRef,
    proveniencia: dict,
) -> UUID:
    with sessao("projetos", USUARIO_SISTEMA) as s:
        _projeto(s, projeto_id)
        linha = ArtefatoModelo(
            projeto_id=projeto_id,
            marco_id=marco_id,
            tipo=tipo,
            nome=nome,
            uri=ref.uri,
            hash_sha256=ref.sha256,
            proveniencia=proveniencia,
        )
        s.add(linha)
        s.flush()
        return linha.id


def artefatos(projeto_id: UUID) -> list[ArtefatoDTO]:
    with sessao("projetos", USUARIO_SISTEMA) as s:
        _projeto(s, projeto_id)
        linhas = s.scalars(
            select(ArtefatoModelo)
            .where(ArtefatoModelo.projeto_id == projeto_id)
            .order_by(ArtefatoModelo.criado_em, ArtefatoModelo.id)
        )
        return [_dto_artefato(a) for a in linhas]


# ---------------------------------------------------------------------------
# Avaliação, lições e próximos passos
# ---------------------------------------------------------------------------
def registrar_avaliacao(
    projeto_id: UUID, criterio: str, nota: Nota, comentario: str | None, usuario: Usuario
) -> AvaliacaoDTO:
    if not (PAPEIS_AVALIADORES & usuario.papeis):
        raise Proibido(f"Avaliar exige um dos papéis {sorted(PAPEIS_AVALIADORES)}")
    with sessao("projetos", usuario.nome) as s:
        _projeto(s, projeto_id)
        linha = AvaliacaoModelo(
            projeto_id=projeto_id,
            criterio=criterio,
            nota=nota,
            comentario=comentario,
            revisor=usuario.nome,
        )
        s.add(linha)
        s.flush()
        s.refresh(linha)
        return _dto_avaliacao(linha)


def avaliacoes_por_playbook(playbook_id: UUID, versao: str) -> list[tuple[UUID, float]]:
    """Média das notas por projeto concluído que usou essa versão do playbook."""
    with sessao("projetos", USUARIO_SISTEMA) as s:
        linhas = s.execute(
            select(AvaliacaoModelo.projeto_id, func.avg(AvaliacaoModelo.nota))
            .join(ProjetoModelo, ProjetoModelo.id == AvaliacaoModelo.projeto_id)
            .where(
                ProjetoModelo.status == "concluido",
                ProjetoModelo.excluido_em.is_(None),
                ProjetoModelo.playbook_id == playbook_id,
                ProjetoModelo.playbook_versao == versao,
            )
            .group_by(AvaliacaoModelo.projeto_id)
            .order_by(AvaliacaoModelo.projeto_id)
        ).all()
        return [(projeto_id, float(media)) for projeto_id, media in linhas]


def registrar_licao(
    projeto_id: UUID, tipo: TipoLicao, descricao: str, playbook_alvo_id: UUID | None
) -> UUID:
    with sessao("projetos", USUARIO_SISTEMA) as s:
        _projeto(s, projeto_id)
        linha = LicaoModelo(
            projeto_id=projeto_id, tipo=tipo, descricao=descricao, playbook_alvo_id=playbook_alvo_id
        )
        s.add(linha)
        s.flush()
        return linha.id


def licoes(projeto_id: UUID) -> list[LicaoDTO]:
    with sessao("projetos", USUARIO_SISTEMA) as s:
        _projeto(s, projeto_id)
        linhas = s.scalars(
            select(LicaoModelo)
            .where(LicaoModelo.projeto_id == projeto_id)
            .order_by(LicaoModelo.criado_em, LicaoModelo.id)
        )
        return [_dto_licao(li) for li in linhas]


def marcar_licao_encaminhada(licao_id: UUID) -> None:
    """Registra que a lição virou proposta de melhoria no playbook (idempotente)."""
    with sessao("projetos", USUARIO_SISTEMA) as s:
        linha = s.scalars(
            select(LicaoModelo).where(LicaoModelo.id == licao_id).with_for_update()
        ).first()
        if linha is None:
            raise NaoEncontrado(f"Lição {licao_id} não encontrada")
        if linha.encaminhada_em is None:
            linha.encaminhada_em = datetime.now(UTC)


def registrar_proximo_passo(projeto_id: UUID, descricao: str) -> UUID:
    with sessao("projetos", USUARIO_SISTEMA) as s:
        _projeto(s, projeto_id)
        linha = ProximoPassoModelo(projeto_id=projeto_id, descricao=descricao)
        s.add(linha)
        s.flush()
        return linha.id


def proximos_passos(projeto_id: UUID) -> list[ProximoPassoDTO]:
    with sessao("projetos", USUARIO_SISTEMA) as s:
        _projeto(s, projeto_id)
        linhas = s.scalars(
            select(ProximoPassoModelo)
            .where(ProximoPassoModelo.projeto_id == projeto_id)
            .order_by(ProximoPassoModelo.criado_em, ProximoPassoModelo.id)
        )
        return [_dto_passo(p) for p in linhas]


def vincular_ideia_gerada(proximo_passo_id: UUID, ideia_id: UUID) -> None:
    """Liga o próximo passo à ideia derivada dele (idempotente para a mesma ideia)."""
    with sessao("projetos", USUARIO_SISTEMA) as s:
        passo = _passo(s, proximo_passo_id)
        if passo.ideia_gerada_id == ideia_id:
            return
        if passo.ideia_gerada_id is not None:
            raise RegraViolada(
                f"O próximo passo {proximo_passo_id} já gerou a ideia {passo.ideia_gerada_id}"
            )
        passo.ideia_gerada_id = ideia_id
        s.flush()  # CHECK: descartado e vinculado são excludentes; FK: a ideia precisa existir


def descartar_proximo_passo(id: UUID, motivo: str, usuario: str) -> ProximoPassoDTO:
    motivo = motivo.strip()
    if not motivo:
        raise RegraViolada("Descartar um próximo passo exige motivo")
    with sessao("projetos", usuario) as s:
        passo = _passo(s, id)
        if passo.descartado_motivo is not None:
            raise RegraViolada(f"O próximo passo {id} já foi descartado")
        passo.descartado_motivo = motivo
        s.flush()  # CHECK: não se descarta um passo que já gerou ideia
        return _dto_passo(passo)
