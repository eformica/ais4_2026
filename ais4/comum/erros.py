from sqlalchemy.exc import DBAPIError

# check_violation, unique_violation, foreign_key_violation, exclusion_violation
SQLSTATE_REGRA_VIOLADA = frozenset({"23514", "23505", "23503", "23P01"})


class RegraViolada(Exception):
    """Uma regra de negócio garantida pelo banco (CHECK, trigger, unicidade, FK) foi violada."""

    def __init__(self, mensagem: str):
        super().__init__(mensagem)
        self.mensagem = mensagem


class NaoEncontrado(Exception):
    """A entidade pedida não existe (ou está logicamente excluída)."""


class Proibido(Exception):
    """O usuário não tem permissão para a ação pedida."""


def traduzir(exc: DBAPIError) -> Exception:
    """Converte erros do driver em erros de domínio; o que não é mapeado volta como está."""
    original = exc.orig
    if getattr(original, "sqlstate", None) in SQLSTATE_REGRA_VIOLADA:
        diag = getattr(original, "diag", None)
        mensagem = getattr(diag, "message_primary", None) or str(original)
        return RegraViolada(mensagem)
    return exc
