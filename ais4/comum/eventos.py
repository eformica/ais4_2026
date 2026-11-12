"""Publicador do outbox: entrega eventos dos módulos aos handlers de outros módulos (D4).

Entrega pelo menos uma vez: o evento só é marcado como publicado depois que todos os handlers
assinados passaram, então um handler pode rodar mais de uma vez e precisa ser idempotente.
Um evento cujo handler falha `AIS4_MAX_TENTATIVAS_EVENTO` vezes é estacionado (`estacionado_em`)
para não travar o outbox; reprocessá-lo é uma decisão humana (docs/piloto.md, seção 5).
Este módulo não importa módulos de domínio; cada módulo com handlers expõe `registrar()`.
"""

import importlib
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from ais4.config import obter_config
from ais4.db import Modulo, sessao

log = logging.getLogger(__name__)

USUARIO_PUBLICADOR = "sistema:outbox"
SCHEMAS_COM_OUTBOX: tuple[Modulo, ...] = ("playbooks", "acervo", "projetos", "criacao", "execucao")
MODULOS_COM_HANDLERS = (
    "ais4.acervo.handlers",
    "ais4.execucao.handlers",
    "ais4.processos.retroalimentacao",
)
_TABELA_INEXISTENTE = "42P01"  # undefined_table
LIMITE_ERRO = 2000  # caracteres de `ultimo_erro` guardados no outbox


@dataclass
class Evento:
    schema: Modulo
    id: int
    agregado_id: UUID
    tipo: str
    payload: dict


@dataclass
class _Assinatura:
    schema: Modulo
    tipo: str
    handler: Callable[[Evento], None]
    filtro: Callable[[Evento], bool] = field(default=lambda e: True)


_assinaturas: list[_Assinatura] = []


def assinar(
    schema: Modulo,
    tipo: str,
    handler: Callable[[Evento], None],
    filtro: Callable[[Evento], bool] = lambda e: True,
) -> None:
    """Registra `handler` para eventos `tipo` do outbox de `schema` que passem em `filtro`.

    Idempotente: assinar de novo o mesmo (schema, tipo, handler) não duplica a assinatura.
    """
    if any(a.schema == schema and a.tipo == tipo and a.handler is handler for a in _assinaturas):
        return
    _assinaturas.append(_Assinatura(schema, tipo, handler, filtro))


@contextmanager
def isolado(*, vazio: bool = False) -> Iterator[None]:
    """Para testes: guarda o registro de handlers e o restaura ao sair (`vazio` o esvazia antes)."""
    guardado = list(_assinaturas)
    if vazio:
        _assinaturas.clear()
    try:
        yield
    finally:
        _assinaturas[:] = guardado


def carregar_handlers() -> None:
    """Importa os módulos que têm handlers e registra as suas assinaturas."""
    for nome in MODULOS_COM_HANDLERS:
        importlib.import_module(nome).registrar()


def _entregar(evento: Evento) -> None:
    interessadas = [a for a in _assinaturas if a.schema == evento.schema and a.tipo == evento.tipo]
    for a in interessadas:
        if a.filtro(evento):
            a.handler(evento)


def publicar_pendentes(schema: Modulo, lote: int = 100) -> int:
    """Entrega aos handlers até `lote` eventos pendentes do outbox de `schema`, em ordem.

    A transação com as linhas travadas (`FOR UPDATE SKIP LOCKED`) fica aberta enquanto os
    handlers rodam (eles usam os engines de outros módulos). Só marca `publicado_em` nos eventos
    cujos handlers passaram todos. Quando um handler falha, incrementa `tentativas` e grava
    `ultimo_erro`; se ainda não atingiu `AIS4_MAX_TENTATIVAS_EVENTO`, para o lote para preservar
    a ordem (o evento será tentado de novo na próxima volta); se atingiu, estaciona o evento
    (`estacionado_em`), registra ERROR e segue com os seguintes. Eventos estacionados não são
    mais selecionados. Devolve quantos foram publicados. Evento sem assinante também é marcado
    como publicado.
    """
    if schema not in SCHEMAS_COM_OUTBOX:
        raise ValueError(f"Schema sem outbox: {schema!r}")
    limite = obter_config().max_tentativas_evento
    publicados = 0
    with sessao(schema, USUARIO_PUBLICADOR) as s:
        linhas = s.execute(
            text(
                f"select id, agregado_id, tipo_evento, payload, tentativas from {schema}.outbox "
                "where publicado_em is null and estacionado_em is null "
                "order by id for update skip locked limit :lote"
            ),
            {"lote": lote},
        ).all()
        for id_, agregado_id, tipo, payload, tentativas in linhas:
            try:
                _entregar(Evento(schema, id_, agregado_id, tipo, payload))
            except Exception as exc:
                tentativas += 1
                erro = f"{type(exc).__name__}: {exc}"[:LIMITE_ERRO]
                estacionar = tentativas >= limite
                s.execute(
                    text(
                        f"update {schema}.outbox set tentativas = :tentativas, "
                        "ultimo_erro = :erro, "
                        "estacionado_em = case when :estacionar then now() end "
                        "where id = :id"
                    ),
                    {"tentativas": tentativas, "erro": erro, "estacionar": estacionar, "id": id_},
                )
                if not estacionar:
                    log.exception(
                        "Falha ao publicar o evento %s (%s) do outbox de %s (tentativa %d de %d)",
                        id_,
                        tipo,
                        schema,
                        tentativas,
                        limite,
                    )
                    break
                log.exception(  # nível ERROR, com o traceback
                    "Evento %s (%s) do outbox de %s estacionado depois de %d falhas: %s. "
                    "Corrija a causa e reprocesse (docs/piloto.md, seção 5).",
                    id_,
                    tipo,
                    schema,
                    tentativas,
                    erro,
                )
                continue
            s.execute(
                text(f"update {schema}.outbox set publicado_em = now() where id = :id"), {"id": id_}
            )
            publicados += 1
    return publicados


def _publicar_se_existir(schema: Modulo) -> int:
    try:
        return publicar_pendentes(schema)
    except ProgrammingError as exc:
        if getattr(exc.orig, "sqlstate", None) == _TABELA_INEXISTENTE:
            log.debug("Schema %s ainda não tem outbox; ignorado", schema)
        else:
            log.exception("Erro ao ler o outbox de %s", schema)
        return 0
    except Exception:
        log.exception("Erro ao ler o outbox de %s", schema)
        return 0


def loop(intervalo_s: float = 2.0, parar: threading.Event | None = None) -> None:
    """Publica os outboxes de todos os módulos até `parar` ser acionado.

    Dorme `intervalo_s` quando uma volta não publicou nada; enquanto houver eventos, segue direto.
    """
    parar = parar or threading.Event()
    while not parar.is_set():
        total = sum(_publicar_se_existir(schema) for schema in SCHEMAS_COM_OUTBOX)
        if total == 0:
            parar.wait(intervalo_s)
