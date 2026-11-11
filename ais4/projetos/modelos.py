"""Modelos SQLAlchemy mapeando as tabelas existentes do schema `projetos` (sem autogenerate)."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CHAR, Boolean, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "projetos"


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


class ProjetoModelo(Base):
    __tablename__ = "projeto"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = _uuid_pk()
    titulo: Mapped[str] = mapped_column(Text)
    ideia_origem_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    playbook_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    playbook_versao: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="aprovado")
    motivo_status: Mapped[str | None] = mapped_column(Text)
    dono: Mapped[str] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
    atualizado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
    excluido_em: Mapped[datetime | None]


class EspecificacaoModelo(Base):
    __tablename__ = "especificacao"
    __table_args__ = {"schema": SCHEMA}

    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    versao: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    conteudo: Mapped[dict] = mapped_column(JSONB)
    justificativa: Mapped[str | None] = mapped_column(Text)
    aprovado_por: Mapped[str] = mapped_column(Text)
    aprovado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class MarcoModelo(Base):
    __tablename__ = "marco"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = _uuid_pk()
    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    codigo: Mapped[str] = mapped_column(Text)
    ordem: Mapped[int] = mapped_column(SmallInteger)
    nome: Mapped[str] = mapped_column(Text)
    etapa_codigo: Mapped[str | None] = mapped_column(Text)
    criterio_saida: Mapped[str] = mapped_column(Text)
    exige_gate: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    papel_revisor: Mapped[str | None] = mapped_column(Text)
    revisor: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pendente")
    data_planejada: Mapped[date | None]
    data_real: Mapped[date | None]
    decisao_gate: Mapped[str | None] = mapped_column(Text)
    decidido_em: Mapped[datetime | None]


class ResponsavelModelo(Base):
    __tablename__ = "responsavel"
    __table_args__ = {"schema": SCHEMA}

    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    papel: Mapped[str] = mapped_column(Text, primary_key=True)
    pessoa: Mapped[str] = mapped_column(Text)


class ArtefatoModelo(Base):
    __tablename__ = "artefato"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = _uuid_pk()
    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    marco_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    tipo: Mapped[str] = mapped_column(Text)
    nome: Mapped[str] = mapped_column(Text)
    uri: Mapped[str] = mapped_column(Text)
    hash_sha256: Mapped[str] = mapped_column(CHAR(64))
    proveniencia: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AvaliacaoModelo(Base):
    __tablename__ = "avaliacao"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = _uuid_pk()
    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    criterio: Mapped[str] = mapped_column(Text)
    nota: Mapped[int] = mapped_column(SmallInteger)
    comentario: Mapped[str | None] = mapped_column(Text)
    revisor: Mapped[str] = mapped_column(Text)
    avaliado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class LicaoModelo(Base):
    __tablename__ = "licao_aprendida"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = _uuid_pk()
    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    tipo: Mapped[str] = mapped_column(Text)
    descricao: Mapped[str] = mapped_column(Text)
    playbook_alvo_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    encaminhada_em: Mapped[datetime | None]
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class ProximoPassoModelo(Base):
    __tablename__ = "proximo_passo"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = _uuid_pk()
    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    descricao: Mapped[str] = mapped_column(Text)
    ideia_gerada_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    descartado_motivo: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
