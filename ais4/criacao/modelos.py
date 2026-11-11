"""Modelos do módulo criacao: tabelas existentes (sem autogenerate) e o resultado da verificação."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "criacao"

StatusFonte = Literal["acessivel", "inacessivel", "nao_localizada"]


class FonteVerificada(BaseModel):
    """Resultado da verificação de uma fonte candidata (passo 3 do fluxo 4.2)."""

    nome: str
    url: str | None = None
    status: StatusFonte
    cobertura: dict = Field(default_factory=dict)
    evidencia: str

    @model_validator(mode="after")
    def _acessivel_tem_url(self) -> Self:
        if self.status == "acessivel" and not (self.url or "").strip():
            raise ValueError("uma fonte acessível precisa da URL que foi testada")
        return self


class Base(DeclarativeBase):
    pass


class PropostaModelo(Base):
    __tablename__ = "proposta"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    ideia_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    playbook_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    playbook_versao: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="rascunho")
    conteudo: Mapped[dict | None] = mapped_column(JSONB)
    iteracoes: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    motivo: Mapped[str | None] = mapped_column(Text)
    solicitado_por: Mapped[str] = mapped_column(Text)
    decidido_por: Mapped[str | None] = mapped_column(Text)
    decidido_em: Mapped[datetime | None]
    projeto_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
    atualizado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class FonteVerificadaModelo(Base):
    __tablename__ = "fonte_verificada"
    __table_args__ = {"schema": SCHEMA}

    proposta_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    nome: Mapped[str] = mapped_column(Text, primary_key=True)
    url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    cobertura: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    evidencia: Mapped[str] = mapped_column(Text)
    testado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))
