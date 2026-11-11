"""CLI: `python -m ais4 {api,migrar,worker-outbox,motor-criacao,worker-execucao,revisao-acervo}`."""

import argparse
import logging
import sys
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config as AlembicConfig

from ais4.comum import eventos

RAIZ = Path(__file__).resolve().parent.parent  # diretório src/
NAO_IMPLEMENTADOS = ("motor-criacao", "worker-execucao", "revisao-acervo")


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


def _nao_implementado(args: argparse.Namespace) -> int:
    print(f"{args.comando}: não implementado", file=sys.stderr)
    return 2


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

    for nome in NAO_IMPLEMENTADOS:
        sub.add_parser(nome).set_defaults(funcao=_nao_implementado)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.funcao(args)


if __name__ == "__main__":
    sys.exit(main())
