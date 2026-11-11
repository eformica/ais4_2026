"""Modelos SQLAlchemy mapeando as tabelas existentes do schema `acervo` (sem autogenerate)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import CHAR, Computed, Float, Numeric, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "acervo"


class Base(DeclarativeBase):
    pass


class IdeiaModelo(Base):
    __tablename__ = "ideia"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    titulo: Mapped[str] = mapped_column(Text)
    tipo: Mapped[str] = mapped_column(Text)
    descricao: Mapped[str] = mapped_column(Text)
    pergunta_norteadora: Mapped[str] = mapped_column(Text)
    dominio: Mapped[list[str]] = mapped_column(ARRAY(Text))
    origem: Mapped[str] = mapped_column(Text)
    origem_url: Mapped[str | None] = mapped_column(Text)
    playbook_sugerido_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    fontes_candidatas: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    restricoes: Mapped[str | None] = mapped_column(Text)
    valor: Mapped[int] = mapped_column(SmallInteger)
    viabilidade: Mapped[int] = mapped_column(SmallInteger)
    prioridade_manual: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    prioridade: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        Computed("COALESCE(prioridade_manual, (valor * viabilidade)::numeric)", persisted=True),
    )
    status: Mapped[str] = mapped_column(Text, server_default="rascunho")
    motivo_status: Mapped[str | None] = mapped_column(Text)
    fundida_em_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # default no banco: acervo.autor_atual()
    criado_por: Mapped[str] = mapped_column(Text, server_default=text("acervo.autor_atual()"))
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
    atualizado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
    excluido_em: Mapped[datetime | None]


class RelacaoModelo(Base):
    __tablename__ = "ideia_relacao"
    __table_args__ = {"schema": SCHEMA}

    ideia_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    relacionada_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tipo: Mapped[str] = mapped_column(Text, primary_key=True)
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class EventoModelo(Base):
    __tablename__ = "ideia_evento"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    ideia_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    evento: Mapped[str] = mapped_column(Text)
    de_status: Mapped[str | None] = mapped_column(Text)
    para_status: Mapped[str | None] = mapped_column(Text)
    motivo: Mapped[str | None] = mapped_column(Text)
    autor: Mapped[str] = mapped_column(Text)
    ocorrido_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class EmbeddingModelo(Base):
    __tablename__ = "ideia_embedding"
    __table_args__ = {"schema": SCHEMA}

    ideia_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    modelo: Mapped[str] = mapped_column(Text)
    conteudo_sha256: Mapped[str] = mapped_column(CHAR(64))
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    gerado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class LacunaModelo(Base):
    __tablename__ = "ideia_lacuna"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    ideia_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    descricao: Mapped[str] = mapped_column(Text)
    classes_requeridas: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text, server_default="aberta")
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
    resolvida_em: Mapped[datetime | None]


class RevisaoDuplicidadeModelo(Base):
    __tablename__ = "ideia_revisao_duplicidade"
    __table_args__ = {"schema": SCHEMA}

    ideia_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    candidata_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    similaridade: Mapped[float] = mapped_column(Float)
    decisao: Mapped[str] = mapped_column(Text)
    revisor: Mapped[str] = mapped_column(Text)
    decidido_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
