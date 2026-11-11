"""Modelos SQLAlchemy mapeando as tabelas existentes do schema `playbooks` (sem autogenerate)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, ForeignKeyConstraint, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "playbooks"


class Base(DeclarativeBase):
    pass


class PlaybookModelo(Base):
    __tablename__ = "playbook"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    codigo: Mapped[str] = mapped_column(Text)
    nome: Mapped[str] = mapped_column(Text)
    descricao: Mapped[str | None] = mapped_column(Text)
    classe_de_problema: Mapped[list[str]] = mapped_column(ARRAY(Text))
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
    excluido_em: Mapped[datetime | None]


class VersaoModelo(Base):
    __tablename__ = "playbook_versao"
    __table_args__ = {"schema": SCHEMA}

    playbook_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    versao: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, server_default="proposto")
    pre_requisitos: Mapped[list] = mapped_column(JSONB)
    template_projeto: Mapped[dict] = mapped_column(JSONB)
    criterios_qualidade: Mapped[list] = mapped_column(JSONB)
    entregaveis: Mapped[list] = mapped_column(JSONB)
    changelog: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
    publicado_em: Mapped[datetime | None]
    # default no banco: playbooks.autor_atual()
    proposto_por: Mapped[str] = mapped_column(Text, server_default=text("playbooks.autor_atual()"))


class EtapaModelo(Base):
    __tablename__ = "etapa"
    __table_args__ = (
        ForeignKeyConstraint(
            ["playbook_id", "versao"],
            [f"{SCHEMA}.playbook_versao.playbook_id", f"{SCHEMA}.playbook_versao.versao"],
        ),
        {"schema": SCHEMA},
    )

    playbook_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    versao: Mapped[str] = mapped_column(Text, primary_key=True)
    codigo: Mapped[str] = mapped_column(Text, primary_key=True)
    ordem: Mapped[int] = mapped_column(SmallInteger)
    nome: Mapped[str] = mapped_column(Text)
    descricao: Mapped[str | None] = mapped_column(Text)
    depende_de: Mapped[list[str]] = mapped_column(ARRAY(Text))
    papel_agente: Mapped[dict] = mapped_column(JSONB)
    entradas: Mapped[list] = mapped_column(JSONB)
    saidas: Mapped[list] = mapped_column(JSONB)
    criterio_saida: Mapped[str] = mapped_column(Text)
    exige_gate: Mapped[bool] = mapped_column(Boolean)
    papel_revisor: Mapped[str | None] = mapped_column(Text)


class PropostaMelhoriaModelo(Base):
    __tablename__ = "proposta_melhoria"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    playbook_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    versao_base: Mapped[str] = mapped_column(Text)
    origem_licao_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    origem_projeto_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    descricao: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pendente")
    versao_resultante: Mapped[str | None] = mapped_column(Text)
    decidido_por: Mapped[str | None] = mapped_column(Text)
    decidido_em: Mapped[datetime | None]
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
