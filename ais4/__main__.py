"""CLI: `python -m ais4 {api,migrar,worker-outbox,motor-criacao,worker-execucao,revisao-acervo}`."""

import argparse
import json
import logging
import signal
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config as AlembicConfig

from ais4.acervo.similaridade import VoyageEmbedder
from ais4.comum import eventos
from ais4.comum.erros import RegraViolada
from ais4.criacao import motor
from ais4.execucao import orquestrador
from ais4.llm.cliente import ClienteAnthropic
from ais4.playbooks import registro
from ais4.playbooks import servico as playbooks
from ais4.processos import atualizacao_acervo

RAIZ = Path(__file__).resolve().parent.parent  # diretório src/
log = logging.getLogger("ais4")

# Processos que executam playbooks: antes de subir, as classes precisam bater com o banco.
COMANDOS_QUE_SINCRONIZAM = ("api", "worker-outbox", "motor-criacao", "worker-execucao")
USUARIO_INICIALIZACAO = "sistema:inicializacao"
SAIDA_PLAYBOOK_DIVERGENTE = 3


def _configurar_log() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


def _sincronizar_playbooks() -> int:
    """Sincroniza cada classe registrada com o banco (insere versões novas como `proposto`).

    Uma versão já publicada que difere da classe impede a subida (código 3): o orquestrador
    executaria as linhas do banco, não a classe editada.
    """
    for pb in registro.todos():
        try:
            playbooks.sincronizar(pb, USUARIO_INICIALIZACAO)
        except RegraViolada as erro:
            log.error("Playbook %s %s difere do banco: %s", pb.id, pb.versao, erro)
            print(
                f"ERRO: a classe do playbook {pb.id} {pb.versao} difere do que está no banco "
                f"({erro}; os campos divergentes estão no log acima). Uma versão publicada não "
                "muda: desfaça a edição ou publique-a como nova versão (semver).",
                file=sys.stderr,
            )
            return SAIDA_PLAYBOOK_DIVERGENTE
    return 0


def _api(args: argparse.Namespace) -> int:
    uvicorn.run("ais4.app:criar_app", factory=True, host=args.host, port=args.porta)
    return 0


def _migrar(_: argparse.Namespace) -> int:
    cfg = AlembicConfig(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "migracoes"))
    cfg.set_main_option("version_locations", str(RAIZ / "migracoes" / "versoes"))
    command.upgrade(cfg, "head")
    return 0


def _worker_outbox(_: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    eventos.carregar_handlers()
    try:
        eventos.loop()
    except KeyboardInterrupt:
        pass
    return 0


def _rodar_ate_sinal(rodar) -> int:
    """Roda `rodar(parar)` com SIGINT/SIGTERM acionando `parar`; restaura os handlers ao sair.

    O trabalho em andamento termina e o laço sai. No SIGINT, um segundo Ctrl+C interrompe de
    verdade (KeyboardInterrupt): quem roda devolve/encerra o que estava fazendo antes de propagar.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parar = threading.Event()

    def pedir_parada(signum, _frame) -> None:
        parar.set()
        if signum == signal.SIGINT:
            signal.signal(signal.SIGINT, signal.default_int_handler)

    anteriores = {}
    for sinal in (signal.SIGINT, signal.SIGTERM):
        try:
            anteriores[sinal] = signal.signal(sinal, pedir_parada)
        except (ValueError, OSError):  # fora da thread principal ou sinal sem suporte
            pass
    try:
        rodar(parar)
    except KeyboardInterrupt:
        pass
    finally:
        for sinal, anterior in anteriores.items():
            signal.signal(sinal, anterior)
    return 0


def _motor_criacao(args: argparse.Namespace) -> int:
    # `executar_ciclo` devolve a proposta antes de propagar uma interrupção.
    return _rodar_ate_sinal(
        lambda parar: motor.loop(
            ClienteAnthropic(),
            args.intervalo,
            parar=parar,
            usuario_sistema=motor.USUARIO_SISTEMA,
        )
    )


def _worker_execucao(args: argparse.Namespace) -> int:
    # A tentativa em andamento termina; numa interrupção ela é marcada `falhou` ("interrompido").
    return _rodar_ate_sinal(
        lambda parar: orquestrador.loop(ClienteAnthropic(), args.intervalo, parar=parar)
    )


def _revisao_acervo(_: argparse.Namespace) -> int:
    """Processo 1.2: reindexa e imprime o relatório (JSON) para o curador; não altera ideias."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    relatorio = atualizacao_acervo.revisao_periodica("sistema:revisao-acervo", VoyageEmbedder())
    print(json.dumps(asdict(relatorio), default=str, ensure_ascii=False, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ais4")
    sub = parser.add_subparsers(dest="comando", required=True)

    api = sub.add_parser("api", help="sobe a API (uvicorn)")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--porta", type=int, default=8000)
    api.set_defaults(funcao=_api)

    migrar = sub.add_parser("migrar", help="aplica as migrações (alembic upgrade head)")
    migrar.set_defaults(funcao=_migrar)

    outbox = sub.add_parser("worker-outbox", help="publica os eventos dos outboxes (loop)")
    outbox.set_defaults(funcao=_worker_outbox)

    criacao = sub.add_parser("motor-criacao", help="roda o fluxo de criação de projetos (loop)")
    criacao.add_argument("--intervalo", type=float, default=30.0, help="segundos entre ciclos")
    criacao.set_defaults(funcao=_motor_criacao)

    execucao = sub.add_parser("worker-execucao", help="executa as etapas dos projetos (loop)")
    execucao.add_argument("--intervalo", type=float, default=5.0, help="segundos entre ciclos")
    execucao.set_defaults(funcao=_worker_execucao)

    revisao = sub.add_parser(
        "revisao-acervo", help="revisão periódica do acervo: imprime o relatório em JSON"
    )
    revisao.set_defaults(funcao=_revisao_acervo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.comando in COMANDOS_QUE_SINCRONIZAM:
        _configurar_log()
        codigo = _sincronizar_playbooks()
        if codigo:
            return codigo
    return args.funcao(args)


if __name__ == "__main__":
    sys.exit(main())
