"""Orquestrador do Motor de Execução (spec 7, D7): DAG de etapas, tentativas, gates e orçamento.

A fila é a própria tabela `execucao.tarefa` (uma linha por tentativa). Uma tentativa é
reservada com lease de 30 minutos, renovado a cada mensagem do modelo e em volta de cada script;
um worker que morre deixa o lease vencer e `recuperar_expiradas` a encerra como `falhou`
(`lease_expirado`), com nova tentativa. Pausar ou cancelar o projeto cancela as tentativas
abertas, inclusive a que está rodando: o worker para na próxima mensagem sem gravar nada. A tentativa aprovada pelo revisor vai a `concluida` mesmo em etapa com gate: quem
carrega o gate é o marco (`aguardando_gate`), decidido por um humano (R21).

Limite de tentativas (R24): conta as falhas consecutivas da etapa desde a última entrada do
projeto em `em_execucao`, sem as falhas transitórias da API; a retomada abre uma nova rodada.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import anthropic

from ais4.comum.dto import MarcoDTO, ProjetoDTO, TarefaDTO
from ais4.comum.erros import NaoEncontrado, RegraViolada
from ais4.config import obter_config
from ais4.execucao import orcamento, servico
from ais4.execucao.artefatos import Armazem
from ais4.execucao.dag import estado_por_etapa, etapas_prontas
from ais4.execucao.execucao_tarefa import (
    ambiente_da_execucao,
    artefatos_disponiveis,
    caminho_do_workspace,
    itens_de_feedback,
    mensagem_do_executor,
    refs_das_entradas,
    registrar_artefatos,
    saidas_da_tarefa,
    semente,
)
from ais4.execucao.ferramentas import (
    ContextoTarefa,
    materializar_entradas,
    montar_ferramentas,
    usa_sandbox,
)
from ais4.execucao.revisor import revisar
from ais4.execucao.sandbox import Sandbox
from ais4.llm.cliente import ClienteLLM, OrcamentoExcedido, RespostaInvalida, Uso
from ais4.playbooks import registro
from ais4.playbooks import servico as playbooks
from ais4.playbooks.base import Etapa, Playbook
from ais4.projetos import servico as projetos

__all__ = [
    "LEASE_S",
    "TarefaPerdida",
    "ao_reprovar_gate",
    "ciclo",
    "executar_tarefa",
    "loop",
    "passo_projeto",
    "recuperar_expiradas",
    "semente",
]

log = logging.getLogger(__name__)

USUARIO = servico.USUARIO_SISTEMA
# Renovado a cada mensagem do modelo e antes/depois de cada script no sandbox. Precisa superar o
# maior intervalo sem renovação: timeout do sandbox (900 s) + margem do kill (30 s) + a latência
# de uma resposta do LLM. Com menos, outro worker varreria uma tentativa saudável.
LEASE_S = 1800
STATUS_ATIVOS = ("pronta", "em_execucao", "validando", "aguardando_gate")
LIMITE_ERRO = 2000  # caracteres de `tarefa.erro`

ERRO_REPROVADA = "reprovada_pelo_revisor"
ERRO_TRANSITORIO = "api_transitoria"
ERRO_SISTEMICO = "api_sistemica"
ERRO_INTERROMPIDO = "interrompido"
ERROS_TRANSITORIOS = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,  # inclui 529 overloaded (OverloadedError)
    anthropic.APIConnectionError,  # inclui timeout
)
ERROS_SISTEMICOS = (
    anthropic.BadRequestError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
)
STATUS_SOBRECARGA = 529


class TarefaPerdida(Exception):
    """A tentativa não é mais deste worker (lease varrido, tarefa cancelada ou projeto fora de
    `em_execucao`): o worker para sem gravar resultado nenhum."""


@dataclass(frozen=True)
class _Projeto:
    dto: ProjetoDTO
    playbook: Playbook
    etapas: list[Etapa]

    def etapa(self, codigo: str) -> Etapa:
        etapa = next((e for e in self.etapas if e.codigo == codigo), None)
        if etapa is None:
            raise NaoEncontrado(f"Etapa {codigo!r} não existe no playbook do projeto")
        return etapa


def _carregar(projeto_id: UUID) -> _Projeto:
    projeto = projetos.obter(projeto_id)
    versao = next(
        (
            v
            for v in playbooks.listar_versoes()
            if v.playbook_id == projeto.playbook_id and v.versao == projeto.playbook_versao
        ),
        None,
    )
    if versao is None:
        raise NaoEncontrado(
            f"Playbook {projeto.playbook_id} v{projeto.playbook_versao} do projeto não encontrado"
        )
    return _Projeto(
        dto=projeto,
        playbook=registro.obter(versao.codigo, versao.versao),
        etapas=playbooks.etapas(projeto.playbook_id, projeto.playbook_versao),
    )


def _ultimas(tarefas: list[TarefaDTO]) -> dict[str, TarefaDTO]:
    ultimas: dict[str, TarefaDTO] = {}
    for t in tarefas:
        if t.etapa_codigo not in ultimas or t.tentativa > ultimas[t.etapa_codigo].tentativa:
            ultimas[t.etapa_codigo] = t
    return ultimas


def _modelo(papel: str) -> str:
    cfg = obter_config()
    return cfg.modelo_por_papel.get(papel, cfg.modelo_padrao)


# ---------------------------------------------------------------------------
# Projeto e marcos
# ---------------------------------------------------------------------------
def _pausar(projeto_id: UUID, motivo: str) -> None:
    log.warning("Projeto %s pausado: %s", projeto_id, motivo)
    try:
        projetos.transicionar(projeto_id, "pausado", USUARIO, motivo)
    except RegraViolada as erro:  # já pausado/cancelado por outro caminho
        log.warning("Não foi possível pausar o projeto %s: %s", projeto_id, erro)


def _mover_marco(projeto_id: UUID, codigo: str, status: str) -> None:
    try:
        projetos.atualizar_marco(projeto_id, codigo, status, USUARIO)
    except RegraViolada as erro:  # ex.: projeto pausado entre a leitura e a escrita
        log.warning("Marco %s do projeto %s não foi para %s: %s", codigo, projeto_id, status, erro)


def _marco_em_andamento(projeto_id: UUID, etapa_codigo: str) -> None:
    marco = projetos.marco_por_etapa(projeto_id, etapa_codigo)
    if marco is not None and marco.status in ("pendente", "reprovado"):
        _mover_marco(projeto_id, marco.codigo, "em_andamento")


def _fechar_marco(projeto_id: UUID, marco: MarcoDTO | None) -> None:
    """Etapa concluída: marco com gate vai a `aguardando_gate`; sem gate, a `aprovado`."""
    if marco is None or marco.status not in ("pendente", "em_andamento"):
        return
    if marco.status == "pendente":
        _mover_marco(projeto_id, marco.codigo, "em_andamento")
    _mover_marco(projeto_id, marco.codigo, "aguardando_gate" if marco.exige_gate else "aprovado")


# ---------------------------------------------------------------------------
# Tentativas
# ---------------------------------------------------------------------------
def _nova_tentativa(
    projeto: _Projeto, etapa: Etapa, tarefas: list[TarefaDTO], feedback: list | dict | None
) -> TarefaDTO | None:
    """Cria a próxima tentativa `pronta` da etapa e põe o marco em andamento.

    `None` se outra tentativa já está ativa (outro worker chegou antes; índice único).
    """
    pid = projeto.dto.id
    proxima = 1 + max((t.tentativa for t in tarefas if t.etapa_codigo == etapa.codigo), default=0)
    concluidas = {t.id for t in tarefas if t.status == "concluida"}
    entradas = {
        "espec_versao": projetos.obter_especificacao(pid).versao,
        "artefatos": artefatos_disponiveis(pid, projeto.etapas, etapa.codigo, concluidas),
    }
    try:
        tarefa = servico.criar_tarefa(
            pid,
            etapa.codigo,
            etapa.papel_agente.papel,
            _modelo(etapa.papel_agente.papel),
            entradas,
            semente(pid, etapa.codigo, proxima),
            feedback=feedback or None,
        )
    except RegraViolada as erro:
        log.info("Etapa %s do projeto %s já tem tentativa ativa: %s", etapa.codigo, pid, erro)
        return None
    _marco_em_andamento(pid, etapa.codigo)
    return tarefa


def _falhas_consecutivas(tarefas: list[TarefaDTO], etapa_codigo: str, desde: datetime) -> int:
    """Falhas seguidas da etapa (da mais recente para trás) criadas desde `desde`.

    Falha transitória da API e tentativa cancelada não contam nem interrompem a sequência.
    """
    contadas = 0
    for t in sorted(
        (t for t in tarefas if t.etapa_codigo == etapa_codigo and t.criado_em >= desde),
        key=lambda t: t.tentativa,
        reverse=True,
    ):
        if t.status == "cancelada" or (t.erro or "").startswith(ERRO_TRANSITORIO):
            continue
        if t.status != "falhou":
            break
        contadas += 1
    return contadas


def _nova_tentativa_ou_pausar(
    projeto_id: UUID, etapa_codigo: str, feedback: list | dict | None
) -> TarefaDTO | None:
    """Depois de uma falha: nova tentativa, ou pausa se o limite de tentativas foi atingido."""
    projeto = _carregar(projeto_id)
    if projeto.dto.status != "em_execucao":
        return None  # a retomada (passo_projeto) abre a próxima tentativa
    tarefas = servico.tarefas(projeto_id)
    desde = projetos.ultima_entrada_em_execucao(projeto_id)
    if _falhas_consecutivas(tarefas, etapa_codigo, desde) >= obter_config().max_tentativas_etapa:
        _pausar(projeto_id, f"etapa {etapa_codigo}: tentativas esgotadas")
        return None
    return _nova_tentativa(projeto, projeto.etapa(etapa_codigo), tarefas, feedback)


def _falhar(
    tarefa: TarefaDTO, erro: str, feedback: list | dict | None, ambiente: dict | None = None
) -> bool:
    """`-> falhou`. `False` se a tarefa já é terminal (o registro de quem a encerrou fica)."""
    try:
        servico.finalizar(
            tarefa.id, "falhou", erro=erro[:LIMITE_ERRO], feedback=feedback, ambiente=ambiente
        )
    except RegraViolada as motivo:
        log.warning("Tarefa %s não foi marcada como falhou (%s): %s", tarefa.id, erro, motivo)
        return False
    return True


# ---------------------------------------------------------------------------
# passo_projeto
# ---------------------------------------------------------------------------
def _reparar_marcos(
    projeto: _Projeto, ultimas: dict[str, TarefaDTO], marcos: list[MarcoDTO]
) -> bool:
    """Completa o que uma queda entre dois passos deixou pela metade. `True` se mexeu em algo.

    Tentativa concluída com o marco ainda pendente/em andamento fecha o marco; marco reprovado
    sem tentativa nova (o handler não rodou, ou rodou com o projeto pausado) reabre a etapa.
    """
    mexeu = False
    for marco in marcos:
        ultima = ultimas.get(marco.etapa_codigo or "")
        if ultima is None:
            continue
        if marco.status == "reprovado":
            ao_reprovar_gate(projeto.dto.id, marco.codigo)
            mexeu = True
        elif ultima.status == "concluida" and marco.status in ("pendente", "em_andamento"):
            _fechar_marco(projeto.dto.id, marco)
            mexeu = True
    return mexeu


def passo_projeto(projeto_id: UUID, cliente: ClienteLLM | None = None) -> list[UUID]:
    """Despacha o que o DAG permite e devolve os ids das tentativas criadas.

    Só age em projeto `em_execucao`: cria a tentativa `pronta` de cada etapa pronta (marco em
    andamento), reabre etapas cuja última tentativa falhou (respeitando o limite) e, com todas as
    etapas `concluida`, leva o projeto a `em_revisao`. Não chama o LLM (isso é de
    `executar_tarefa`): `cliente` segue a assinatura do plano e hoje não é usado.
    """
    projeto = _carregar(projeto_id)
    if projeto.dto.status != "em_execucao":
        return []
    tarefas = servico.tarefas(projeto_id)
    ultimas = _ultimas(tarefas)
    marcos = projetos.marcos(projeto_id)
    if _reparar_marcos(projeto, ultimas, marcos):
        projeto = _carregar(projeto_id)
        if projeto.dto.status != "em_execucao":
            return []
        tarefas = servico.tarefas(projeto_id)
        ultimas = _ultimas(tarefas)
        marcos = projetos.marcos(projeto_id)

    estado = estado_por_etapa(projeto.etapas, tarefas, marcos)
    if estado and all(situacao == "concluida" for situacao in estado.values()):
        projetos.transicionar(projeto_id, "em_revisao", USUARIO, "todas as etapas concluídas")
        return []

    criadas: list[UUID] = []
    for codigo, situacao in estado.items():
        if situacao == "falhou":  # queda depois da falha, ou retomada após pausa
            nova = _nova_tentativa_ou_pausar(projeto_id, codigo, ultimas[codigo].feedback)
            if nova is not None:
                criadas.append(nova.id)
            if projetos.obter(projeto_id).status != "em_execucao":
                return criadas
    for codigo in etapas_prontas(projeto.etapas, estado):
        anterior = ultimas.get(codigo)  # cancelada na pausa: herda o feedback pendente
        feedback = anterior.feedback if anterior is not None else None
        nova = _nova_tentativa(projeto, projeto.etapa(codigo), tarefas, feedback)
        if nova is not None:
            criadas.append(nova.id)
    return criadas


# ---------------------------------------------------------------------------
# executar_tarefa
# ---------------------------------------------------------------------------
def _perdida(tarefa_id: UUID) -> TarefaPerdida:
    return TarefaPerdida(f"a tarefa {tarefa_id} não está mais com este worker")


def _garantir_projeto_em_execucao(tarefa: TarefaDTO) -> None:
    status = projetos.obter(tarefa.projeto_id).status
    if status != "em_execucao":
        raise TarefaPerdida(f"o projeto {tarefa.projeto_id} está {status!r}")


def _vigia(tarefa: TarefaDTO) -> Callable[[Uso], None]:
    """`ao_usar` da tarefa: renova o lease, registra a chamada, confere o orçamento e que o
    projeto segue em execução (sem esperar o handler da pausa)."""
    registrar_e_verificar = orcamento.vigiar(tarefa.projeto_id, tarefa.id)

    def ao_usar(uso: Uso) -> None:
        renovou = servico.renovar_lease(tarefa.id, LEASE_S)
        registrar_e_verificar(uso)  # o gasto já aconteceu: registra mesmo sem o lease
        if not renovou:
            raise _perdida(tarefa.id)
        _garantir_projeto_em_execucao(tarefa)

    return ao_usar


class _SandboxComLease:
    """Sandbox da tentativa: renova o lease antes e depois de cada script (um script pode durar
    até o timeout do sandbox, sem nenhuma mensagem do modelo no meio)."""

    def __init__(self, sandbox, tarefa_id: UUID) -> None:
        self._sandbox = sandbox
        self._tarefa_id = tarefa_id

    def disponivel(self) -> bool:
        return self._sandbox.disponivel()

    def executar(self, workspace, script: str, *, rede: bool, timeout_s: int = 900, semente: int):
        if not servico.renovar_lease(self._tarefa_id, LEASE_S):
            raise _perdida(self._tarefa_id)  # o script nem roda; a próxima mensagem para o agente
        try:
            return self._sandbox.executar(
                workspace, script, rede=rede, timeout_s=timeout_s, semente=semente
            )
        finally:
            servico.renovar_lease(self._tarefa_id, LEASE_S)


def _foi_perdida(tarefa: TarefaDTO) -> bool:
    """A tarefa já é terminal ou o projeto saiu de `em_execucao` (R26: o `RegraViolada` de um
    worker zumbi não é erro interno)."""
    return (
        servico.obter_tarefa(tarefa.id).status in ("concluida", "falhou", "cancelada")
        or projetos.obter(tarefa.projeto_id).status != "em_execucao"
    )


def _abandonar(tarefa: TarefaDTO, motivo: Exception) -> None:
    """Para sem gravar resultado. Se o projeto parou e o handler da pausa ainda não cancelou a
    tentativa, o próprio worker a cancela (o lease não fica preso até vencer)."""
    log.warning("Tarefa %s abandonada: %s", tarefa.id, motivo)
    try:
        aberta = servico.obter_tarefa(tarefa.id).status in ("em_execucao", "validando")
        if aberta and projetos.obter(tarefa.projeto_id).status != "em_execucao":
            servico.finalizar(tarefa.id, "cancelada")
    except RegraViolada:  # outro caminho já a encerrou
        pass


def _motivo_orcamento(erro: OrcamentoExcedido) -> str:
    if erro.dimensao is None:
        return f"orçamento: {erro}"
    return f"orçamento: {erro.dimensao} {erro.consumido}/{erro.limite}"


def _transitorio(erro: Exception) -> bool:
    return isinstance(erro, ERROS_TRANSITORIOS) or (
        isinstance(erro, anthropic.APIStatusError) and erro.status_code == STATUS_SOBRECARGA
    )


def _tratar_falha(tarefa: TarefaDTO, erro: Exception, contexto: ContextoTarefa | None) -> None:
    """Classifica a exceção da tentativa e aplica a consequência (spec 7, R24)."""
    pid, codigo = tarefa.projeto_id, tarefa.etapa_codigo
    ambiente = ambiente_da_execucao(contexto) if contexto is not None else None
    pendente = tarefa.feedback  # falha que não é do conteúdo: o feedback anterior continua valendo
    if isinstance(erro, OrcamentoExcedido):
        _falhar(tarefa, f"orcamento: {erro}", pendente, ambiente)
        _pausar(pid, _motivo_orcamento(erro))
    elif isinstance(erro, RespostaInvalida):
        feedback = [
            (
                f"A tentativa anterior terminou sem resultado válido ({erro}). Grave as saídas no "
                "workspace e termine chamando concluir_etapa."
            )
        ]
        if _falhar(tarefa, f"resposta_invalida: {erro}", feedback, ambiente):
            _nova_tentativa_ou_pausar(pid, codigo, feedback)
    elif _transitorio(erro):
        log.warning("Falha transitória da API na tarefa %s: %s", tarefa.id, erro)
        if _falhar(tarefa, f"{ERRO_TRANSITORIO}: {erro}", pendente, ambiente):
            _nova_tentativa_ou_pausar(pid, codigo, pendente)
    elif isinstance(erro, ERROS_SISTEMICOS):
        _falhar(tarefa, f"{ERRO_SISTEMICO}: {erro}", pendente, ambiente)
        _pausar(pid, f"erro sistêmico da API: {erro}")
    else:
        log.error("Erro inesperado na tarefa %s", tarefa.id, exc_info=erro)
        if _falhar(tarefa, f"erro_interno: {type(erro).__name__}: {erro}", pendente, ambiente):
            _nova_tentativa_ou_pausar(pid, codigo, pendente)


def _preparar_contexto(
    tarefa: TarefaDTO, etapa: Etapa, sandbox, armazem: Armazem
) -> ContextoTarefa:
    workspace = caminho_do_workspace(tarefa)
    workspace.mkdir(parents=True, exist_ok=True)
    contexto = ContextoTarefa(
        projeto_id=tarefa.projeto_id,
        tarefa_id=tarefa.id,
        etapa=etapa,
        workspace=workspace,
        sandbox=sandbox,
        armazem=armazem,
        semente=(
            tarefa.semente
            if tarefa.semente is not None
            else semente(tarefa.projeto_id, etapa.codigo, tarefa.tentativa)
        ),
        artefatos_disponiveis=refs_das_entradas(tarefa.entradas),
    )
    materializar_entradas(contexto)
    return contexto


def _executar_reservada(
    tarefa: TarefaDTO, projeto: _Projeto, cliente: ClienteLLM, sandbox, armazem: Armazem
) -> TarefaDTO:
    """Roda uma tentativa já `em_execucao` com lease até um estado final (ou até perdê-la)."""
    pid = tarefa.projeto_id
    etapa = projeto.etapa(tarefa.etapa_codigo)
    contexto: ContextoTarefa | None = None
    try:
        if projetos.obter(pid).status != "em_execucao":  # pausado entre a seleção e a reserva
            servico.finalizar(tarefa.id, "cancelada")
            return servico.obter_tarefa(tarefa.id)
        orcamento.verificar(pid)  # nem a primeira chamada com o orçamento já estourado
        contexto = _preparar_contexto(tarefa, etapa, _SandboxComLease(sandbox, tarefa.id), armazem)
        versao = tarefa.entradas.get("espec_versao")
        ao_usar = _vigia(tarefa)
        cliente.agente(
            papel=etapa.papel_agente.papel,
            sistema=etapa.papel_agente.prompt_base,
            mensagem=mensagem_do_executor(
                etapa,
                projetos.obter_especificacao(pid, versao),
                contexto.artefatos_disponiveis,
                itens_de_feedback(tarefa.feedback),
            ),
            ferramentas=montar_ferramentas(contexto),
            ao_usar=ao_usar,
        )
        if contexto.saida is None:
            raise RespostaInvalida("sem_conclusao", "o agente terminou sem chamar concluir_etapa")

        servico.finalizar(
            tarefa.id,
            "validando",
            saidas=saidas_da_tarefa(contexto),
            ambiente=ambiente_da_execucao(contexto),
        )
        if not servico.renovar_lease(tarefa.id, LEASE_S):
            raise _perdida(tarefa.id)
        _garantir_projeto_em_execucao(tarefa)  # antes de gastar com o revisor
        resultado = revisar(
            projeto.playbook,
            etapa,
            contexto.saida,
            list(projeto.playbook.criterios_qualidade),
            cliente,
            ao_usar,
            armazem=armazem,
        )
        _garantir_projeto_em_execucao(tarefa)  # antes de gravar qualquer resultado
        if resultado.aprovado:
            marco = projetos.marco_por_etapa(pid, etapa.codigo)
            registrar_artefatos(tarefa, contexto, marco.id if marco else None)
            servico.finalizar(tarefa.id, "concluida")
            _fechar_marco(pid, marco)
        elif _falhar(tarefa, ERRO_REPROVADA, resultado.feedback):
            _nova_tentativa_ou_pausar(pid, etapa.codigo, resultado.feedback)
    except TarefaPerdida as erro:
        _abandonar(tarefa, erro)
    except Exception as erro:  # noqa: BLE001 - toda falha vira estado da tentativa, nunca some
        if isinstance(erro, RegraViolada) and _foi_perdida(tarefa):
            _abandonar(tarefa, erro)  # zumbi: o registro de quem encerrou a tarefa fica
        else:
            _tratar_falha(tarefa, erro, contexto)
    except BaseException:  # Ctrl+C, SystemExit: a tentativa não pode ficar presa em execução
        try:
            _falhar(tarefa, ERRO_INTERROMPIDO, tarefa.feedback)
        except Exception:  # sem banco, o lease vence e `recuperar_expiradas` encerra a tarefa
            log.exception("Não foi possível marcar a tarefa %s como interrompida", tarefa.id)
        raise
    return servico.obter_tarefa(tarefa.id)


def _avisar_sem_docker(tarefa_id: UUID, etapa: Etapa) -> None:
    log.warning(
        "Docker indisponível: a tarefa %s (etapa %s, ferramenta python) fica pronta até o "
        "sandbox voltar",
        tarefa_id,
        etapa.codigo,
    )


def executar_tarefa(
    tarefa_id: UUID,
    cliente: ClienteLLM,
    *,
    sandbox=None,
    armazem: Armazem | None = None,
) -> TarefaDTO | None:
    """Reserva (lease de `LEASE_S`) e roda a tentativa; devolve o estado final.

    `None` se a tarefa não foi reservada: não estava `pronta`, o projeto não está em execução,
    outro worker a levou ou a etapa usa o sandbox e o Docker está indisponível. Interrupção
    (`BaseException`) marca a tentativa como `falhou` (`interrompido`) e é relançada.
    """
    tarefa = servico.obter_tarefa(tarefa_id)
    if tarefa.status != "pronta":
        return None
    projeto = _carregar(tarefa.projeto_id)
    if projeto.dto.status != "em_execucao":
        return None
    sandbox = sandbox or Sandbox()
    etapa = projeto.etapa(tarefa.etapa_codigo)
    if usa_sandbox(etapa) and not sandbox.disponivel():
        _avisar_sem_docker(tarefa_id, etapa)
        return None
    if not servico.reservar(tarefa_id, LEASE_S):
        return None
    return _executar_reservada(
        servico.obter_tarefa(tarefa_id), projeto, cliente, sandbox, armazem or Armazem()
    )


# ---------------------------------------------------------------------------
# Gate reprovado, leases vencidos e laço
# ---------------------------------------------------------------------------
def ao_reprovar_gate(projeto_id: UUID, marco: str) -> UUID | None:
    """Gate `marco` (código) reprovado: nova tentativa com `feedback = [decisao_gate]` e o marco
    `reprovado -> em_andamento`. Idempotente; não conta no limite (a tentativa anterior foi
    concluída, então a sequência de falhas recomeça). Com o projeto fora de `em_execucao` não faz
    nada: `passo_projeto` reabre a etapa na retomada. Devolve o id da tentativa criada.
    """
    atual = next((m for m in projetos.marcos(projeto_id) if m.codigo == marco), None)
    if atual is None or atual.status != "reprovado" or atual.etapa_codigo is None:
        return None
    projeto = _carregar(projeto_id)
    if projeto.dto.status != "em_execucao":
        log.info("Gate %s reprovado com o projeto %s fora de execução", marco, projeto_id)
        return None
    tarefas = servico.tarefas(projeto_id)
    ultima = _ultimas(tarefas).get(atual.etapa_codigo)
    criada = None
    if ultima is None or ultima.status not in STATUS_ATIVOS:
        feedback = [atual.decisao_gate] if atual.decisao_gate else None
        nova = _nova_tentativa(projeto, projeto.etapa(atual.etapa_codigo), tarefas, feedback)
        criada = nova.id if nova is not None else None
    _marco_em_andamento(projeto_id, atual.etapa_codigo)  # tarefa primeiro: queda no meio é segura
    return criada


def recuperar_expiradas() -> list[UUID]:
    """Tentativas com lease vencido (worker morto) -> `falhou` (`lease_expirado`) e nova
    tentativa, sujeita ao limite. Devolve os ids das tentativas encerradas."""
    expiradas = servico.expirar_leases()
    for tarefa in expiradas:
        log.warning(
            "Lease vencido: tarefa %s (etapa %s, tentativa %d) encerrada",
            tarefa.id,
            tarefa.etapa_codigo,
            tarefa.tentativa,
        )
        try:
            _nova_tentativa_ou_pausar(tarefa.projeto_id, tarefa.etapa_codigo, tarefa.feedback)
        except Exception:  # um projeto com problema não impede a recuperação dos outros
            log.exception("Falha ao abrir nova tentativa depois do lease da tarefa %s", tarefa.id)
    return [t.id for t in expiradas]


def ciclo(cliente: ClienteLLM, *, sandbox=None, armazem: Armazem | None = None) -> UUID | None:
    """Uma volta: `recuperar_expiradas`, `passo_projeto` em cada projeto em execução e a
    execução de UMA tentativa `pronta` (`FOR UPDATE SKIP LOCKED`). Devolve o id executado."""
    sandbox = sandbox or Sandbox()
    recuperar_expiradas()
    for projeto in projetos.listar("em_execucao", limite=200):
        try:
            passo_projeto(projeto.id, cliente)
        except Exception:
            log.exception("passo do projeto %s falhou", projeto.id)

    candidatas: list[UUID] = []
    carregados: dict[UUID, _Projeto] = {}
    docker: bool | None = None
    for dto in projetos.listar("em_execucao", limite=200):
        carregados[dto.id] = projeto = _carregar(dto.id)
        for tarefa in servico.tarefas(dto.id):
            if tarefa.status != "pronta":
                continue
            etapa = projeto.etapa(tarefa.etapa_codigo)
            if usa_sandbox(etapa):
                if docker is None:  # uma checagem por volta, só se alguma etapa precisa
                    docker = sandbox.disponivel()
                if not docker:
                    _avisar_sem_docker(tarefa.id, etapa)
                    continue
            candidatas.append(tarefa.id)

    tarefa = servico.reservar_proxima(candidatas, LEASE_S)
    if tarefa is None:
        return None
    _executar_reservada(
        tarefa, carregados[tarefa.projeto_id], cliente, sandbox, armazem or Armazem()
    )
    return tarefa.id


def loop(
    cliente: ClienteLLM,
    intervalo_s: float = 5,
    parar: threading.Event | None = None,
    sandbox=None,
    armazem: Armazem | None = None,
) -> None:
    """`ciclo` a cada `intervalo_s` até `parar`. Erro de um ciclo é registrado e não mata o laço;
    a tentativa em andamento termina antes de o laço sair."""
    parar = parar or threading.Event()
    sandbox = sandbox or Sandbox()
    armazem = armazem or Armazem()
    while not parar.is_set():
        try:
            ciclo(cliente, sandbox=sandbox, armazem=armazem)
        except Exception:
            log.exception("ciclo do orquestrador falhou")
        parar.wait(intervalo_s)
