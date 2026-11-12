"""Modelos do módulo execucao: tabelas existentes (sem autogenerate)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, Numeric, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "execucao"


class Base(DeclarativeBase):
    pass


class OrcamentoModelo(Base):
    __tablename__ = "orcamento"
    __table_args__ = {"schema": SCHEMA}

    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    max_tokens: Mapped[int] = mapped_column(BigInteger)
    max_custo_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    prazo: Mapped[datetime | None]


class TarefaModelo(Base):
    __tablename__ = "tarefa"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    etapa_codigo: Mapped[str] = mapped_column(Text)
    tentativa: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(Text, server_default="pronta")
    papel: Mapped[str] = mapped_column(Text)
    modelo: Mapped[str] = mapped_column(Text)
    entradas: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    saidas: Mapped[dict | None] = mapped_column(JSONB)
    feedback: Mapped[dict | list | None] = mapped_column(JSONB)
    erro: Mapped[str | None] = mapped_column(Text)
    ambiente: Mapped[dict | None] = mapped_column(JSONB)
    semente: Mapped[int | None]
    lease_ate: Mapped[datetime | None]
    iniciado_em: Mapped[datetime | None]
    finalizado_em: Mapped[datetime | None]
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class ChamadaLLMModelo(Base):
    """Somente inserção (o banco bloqueia UPDATE e DELETE)."""

    __tablename__ = "chamada_llm"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tarefa_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    proposta_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    projeto_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    ideia_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))  # lógico, sem FK (R30)
    modelo: Mapped[str] = mapped_column(Text)
    tokens_entrada: Mapped[int] = mapped_column(BigInteger)
    tokens_saida: Mapped[int] = mapped_column(BigInteger)
    tokens_cache_leitura: Mapped[int] = mapped_column(BigInteger, server_default="0")
    buscas_web: Mapped[int] = mapped_column(server_default="0")
    custo_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    duracao_ms: Mapped[int]
    request_id: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(server_default=text("now()"))


class ConsumoModelo(Base):
    """View `consumo` (somente leitura): tokens = entrada + saída + leitura de cache."""

    __tablename__ = "consumo"
    __table_args__ = {"schema": SCHEMA}

    projeto_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tokens: Mapped[int] = mapped_column(BigInteger)
    custo_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
