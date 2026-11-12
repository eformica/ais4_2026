"""Tarefas do Motor de Execução: uma linha por tentativa de etapa, fila com lease.

As transições de status são validadas pelo banco (trigger `tarefa_transicao`); transição inválida
vira `RegraViolada`. Reexecutar uma etapa nunca altera a tentativa anterior: `criar_tarefa` insere
`tentativa = max + 1`.
"""

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select, update

from ais4.comum.dto import TarefaDTO
from ais4.comum.erros import NaoEncontrado, RegraViolada
from ais4.db import sessao
from ais4.execucao.modelos import TarefaModelo

USUARIO_SISTEMA = "sistema:execucao"

StatusFinal = Literal["validando", "aguardando_gate", "concluida", "falhou", "cancelada"]
_TERMINAIS = ("concluida", "falhou", "cancelada")
_COM_LEASE = ("em_execucao", "validando")  # o worker detém a tarefa enquanto o lease vale
_ABERTOS = ("pronta", "em_execucao", "validando", "aguardando_gate")
ERRO_LEASE_EXPIRADO = "lease_expirado"


def _dto(t: TarefaModelo) -> TarefaDTO:
    return TarefaDTO(
        id=t.id,
        projeto_id=t.projeto_id,
        etapa_codigo=t.etapa_codigo,
        tentativa=t.tentativa,
        status=t.status,
        papel=t.papel,
        modelo=t.modelo,
        entradas=t.entradas,
        saidas=t.saidas,
        feedback=t.feedback,
        erro=t.erro,
        ambiente=t.ambiente,
        semente=t.semente,
        lease_ate=t.lease_ate,
        iniciado_em=t.iniciado_em,
        finalizado_em=t.finalizado_em,
        criado_em=t.criado_em,
    )


def _lease(lease_s: float):
    if lease_s <= 0:
        raise RegraViolada("O lease precisa ser positivo")
    return func.now() + func.make_interval(0, 0, 0, 0, 0, 0, float(lease_s))


def criar_tarefa(
    projeto_id: UUID,
    etapa_codigo: str,
    papel: str,
    modelo: str,
    entradas: dict,
    semente: int | None,
    feedback: dict | list | None = None,
) -> TarefaDTO:
    """Cria a próxima tentativa (`max + 1`) da etapa, `pronta`. Não apaga as anteriores.

    `feedback` é o que a tentativa precisa corrigir (do revisor, do gate ou da falha anterior).
    Só uma tentativa ativa por etapa (índice `tarefa_ativa_uq`): outra ativa -> `RegraViolada`.
    """
    with sessao("execucao", USUARIO_SISTEMA) as s:
        proxima = (
            select(func.coalesce(func.max(TarefaModelo.tentativa), 0) + 1)
            .where(
                TarefaModelo.projeto_id == projeto_id,
                TarefaModelo.etapa_codigo == etapa_codigo,
            )
            .scalar_subquery()
        )
        tarefa = TarefaModelo(
            projeto_id=projeto_id,
            etapa_codigo=etapa_codigo,
            tentativa=proxima,
            papel=papel,
            modelo=modelo,
            entradas=entradas,
            semente=semente,
            feedback=feedback,
        )
        s.add(tarefa)
        s.flush()
        s.refresh(tarefa)
        return _dto(tarefa)


def obter_tarefa(tarefa_id: UUID) -> TarefaDTO:
    with sessao("execucao", USUARIO_SISTEMA) as s:
        tarefa = s.get(TarefaModelo, tarefa_id)
        if tarefa is None:
            raise NaoEncontrado(f"Tarefa {tarefa_id} não encontrada")
        return _dto(tarefa)


def tarefas(projeto_id: UUID) -> list[TarefaDTO]:
    """Todas as tentativas do projeto, por etapa e tentativa."""
    with sessao("execucao", USUARIO_SISTEMA) as s:
        linhas = s.scalars(
            select(TarefaModelo)
            .where(TarefaModelo.projeto_id == projeto_id)
            .order_by(TarefaModelo.etapa_codigo, TarefaModelo.tentativa)
        )
        return [_dto(t) for t in linhas]


def reservar(tarefa_id: UUID, lease_s: float) -> bool:
    """`pronta -> em_execucao` com lease de `lease_s` segundos, num único UPDATE condicional.

    Devolve `True` se esta chamada ganhou a tarefa; `False` se ela já não estava `pronta`
    (outro worker a levou, ou foi cancelada).
    """
    with sessao("execucao", USUARIO_SISTEMA) as s:
        ganhou = s.execute(
            update(TarefaModelo)
            .where(TarefaModelo.id == tarefa_id, TarefaModelo.status == "pronta")
            .values(status="em_execucao", lease_ate=_lease(lease_s), iniciado_em=func.now())
            .returning(TarefaModelo.id)
        ).first()
        return ganhou is not None


def reservar_proxima(candidatas: list[UUID], lease_s: float) -> TarefaDTO | None:
    """Reserva a mais antiga das `candidatas` ainda `pronta` (`FOR UPDATE SKIP LOCKED`).

    Linhas travadas por outro worker são puladas em vez de esperadas; devolve a tarefa já
    `em_execucao` com lease, ou `None` se nenhuma candidata estava disponível.
    """
    if not candidatas:
        return None
    with sessao("execucao", USUARIO_SISTEMA) as s:
        tarefa = s.scalars(
            select(TarefaModelo)
            .where(TarefaModelo.id.in_(candidatas), TarefaModelo.status == "pronta")
            .order_by(TarefaModelo.criado_em, TarefaModelo.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if tarefa is None:
            return None
        reservada = s.scalars(
            update(TarefaModelo)
            .where(TarefaModelo.id == tarefa.id)
            .values(status="em_execucao", lease_ate=_lease(lease_s), iniciado_em=func.now())
            .returning(TarefaModelo)
            .execution_options(populate_existing=True)
        ).one()
        return _dto(reservada)


def renovar_lease(tarefa_id: UUID, lease_s: float) -> bool:
    """Estende o lease de uma tarefa `em_execucao` ou `validando`.

    `False` se ela já não está em nenhum dos dois (o lease venceu e foi varrido, ou a tarefa foi
    cancelada): o worker perdeu a tarefa e deve parar.
    """
    with sessao("execucao", USUARIO_SISTEMA) as s:
        renovou = s.execute(
            update(TarefaModelo)
            .where(TarefaModelo.id == tarefa_id, TarefaModelo.status.in_(_COM_LEASE))
            .values(lease_ate=_lease(lease_s))
            .returning(TarefaModelo.id)
        ).first()
        return renovou is not None


def finalizar(
    tarefa_id: UUID,
    status: StatusFinal,
    *,
    saidas: dict | None = None,
    erro: str | None = None,
    feedback: dict | list | None = None,
    ambiente: dict | None = None,
) -> TarefaDTO:
    """Sai de `em_execucao` (ou da validação) para `status`.

    Só grava os campos informados. `finalizado_em` é preenchido nos status terminais
    (`concluida`, `falhou`, `cancelada`). O lease segue valendo em `validando` e é limpo nos
    demais status. Transição inválida, ou tarefa já terminal (por exemplo, varrida por lease
    vencido enquanto um worker zumbi ainda rodava): `RegraViolada`.
    """
    valores: dict[str, Any] = {"status": status}
    if status != "validando":
        valores["lease_ate"] = None
    if status in _TERMINAIS:
        valores["finalizado_em"] = func.now()
    for campo, valor in (
        ("saidas", saidas),
        ("erro", erro),
        ("feedback", feedback),
        ("ambiente", ambiente),
    ):
        if valor is not None:
            valores[campo] = valor
    with sessao("execucao", USUARIO_SISTEMA) as s:
        atual = s.scalars(
            select(TarefaModelo).where(TarefaModelo.id == tarefa_id).with_for_update()
        ).first()
        if atual is None:
            raise NaoEncontrado(f"Tarefa {tarefa_id} não encontrada")
        if atual.status in _TERMINAIS:
            raise RegraViolada(
                f"Tarefa {tarefa_id} já terminou ({atual.status}): estado terminal, "
                "status e campos imutáveis"
            )
        tarefa = s.scalars(
            update(TarefaModelo)
            .where(TarefaModelo.id == tarefa_id)
            .values(**valores)
            .returning(TarefaModelo)
            .execution_options(populate_existing=True)
        ).one()
        return _dto(tarefa)


def cancelar_abertas(projeto_id: UUID) -> list[UUID]:
    """Toda tarefa não terminal do projeto (`pronta`, `em_execucao`, `validando`,
    `aguardando_gate`) -> `cancelada`, com o lease limpo. Idempotente.

    Um worker que ainda rode uma delas perde a renovação do lease na próxima mensagem do modelo
    e para sem gravar nada (`renovar_lease` devolve `False`).
    """
    with sessao("execucao", USUARIO_SISTEMA) as s:
        return list(
            s.scalars(
                update(TarefaModelo)
                .where(TarefaModelo.projeto_id == projeto_id, TarefaModelo.status.in_(_ABERTOS))
                .values(status="cancelada")
                .returning(TarefaModelo.id)
            )
        )


def expirar_leases() -> list[TarefaDTO]:
    """Tarefas `em_execucao`/`validando` com lease vencido viram `falhou` (`lease_expirado`).

    Um único UPDATE condicional: uma renovação concorrente que chegue antes vence, e a tarefa
    não é tocada. O `feedback` fica como estava (a falha não foi do conteúdo).
    """
    with sessao("execucao", USUARIO_SISTEMA) as s:
        linhas = s.scalars(
            update(TarefaModelo)
            .where(TarefaModelo.status.in_(_COM_LEASE), TarefaModelo.lease_ate < func.now())
            .values(status="falhou", erro=ERRO_LEASE_EXPIRADO, lease_ate=None)
            .returning(TarefaModelo)
            .execution_options(populate_existing=True)
        )
        return [_dto(t) for t in linhas]
