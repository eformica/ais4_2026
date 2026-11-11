from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ais4.acervo.rotas import router as rotas_acervo
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada, ServicoIndisponivel
from ais4.playbooks.rotas import router as rotas_playbooks
from ais4.projetos.rotas import router as rotas_projetos


def _erro(status: int, mensagem: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"erro": mensagem}, status_code=status, headers=headers)


def criar_app() -> FastAPI:
    app = FastAPI(title="AIS4 - orquestração de projetos de IA")

    @app.exception_handler(RegraViolada)
    def _regra_violada(_: Request, exc: RegraViolada) -> JSONResponse:
        return _erro(409, exc.mensagem)

    @app.exception_handler(NaoEncontrado)
    def _nao_encontrado(_: Request, exc: NaoEncontrado) -> JSONResponse:
        return _erro(404, str(exc))

    @app.exception_handler(Proibido)
    def _proibido(_: Request, exc: Proibido) -> JSONResponse:
        return _erro(403, str(exc))

    @app.exception_handler(ServicoIndisponivel)
    def _indisponivel(_: Request, exc: ServicoIndisponivel) -> JSONResponse:
        return _erro(503, str(exc))

    @app.exception_handler(StarletteHTTPException)
    def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 401 da autenticação e demais HTTPException seguem o mesmo corpo {"erro": ...}
        return _erro(exc.status_code, str(exc.detail), dict(exc.headers or {}))

    @app.get("/saude")
    def saude() -> dict[str, str]:
        return {"status": "ok"}

    # --- ROUTERS DOS MÓDULOS -------------------------------------------------
    # Cada módulo adiciona aqui o seu `app.include_router(...)` quando criar rotas.py.
    app.include_router(rotas_playbooks)
    app.include_router(rotas_acervo)
    app.include_router(rotas_projetos)
    # -------------------------------------------------------------------------

    return app
