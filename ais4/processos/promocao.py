"""Promoção de playbook (spec 6.2): `experimental -> validado` após N projetos concluídos com nota.

O critério é mecânico e vem da configuração (valores provisórios do piloto):
`AIS4_PROMOCAO_MIN_PROJETOS` projetos concluídos com a versão e, em cada um, média das avaliações
>= `AIS4_PROMOCAO_NOTA_MIN`. Revisão humana, teste no projeto de referência e a regra "quem propôs
não promove" não são verificados aqui: o endpoint de promoção as aplica (papel de guardião e
`playbooks.servico.alterar_status`).
"""

from dataclasses import dataclass
from uuid import UUID

from ais4.config import obter_config
from ais4.projetos import servico as projetos


@dataclass(frozen=True)
class Elegibilidade:
    elegivel: bool
    projetos_concluidos: int
    nota_media: float | None  # média das médias por projeto; `None` sem projetos avaliados
    motivo: str


def elegibilidade(playbook_id: UUID, versao: str) -> Elegibilidade:
    """Confere os limiares de promoção para a versão do playbook.

    Só entram os projetos `concluido` que usaram exatamente essa versão e têm avaliação
    (`projetos.avaliacoes_por_playbook`).
    """
    cfg = obter_config()
    por_projeto = projetos.avaliacoes_por_playbook(playbook_id, versao)
    total = len(por_projeto)
    media = sum(nota for _, nota in por_projeto) / total if total else None

    problemas: list[str] = []
    if total < cfg.promocao_min_projetos:
        problemas.append(
            f"{total} projetos concluídos com avaliação; o mínimo de "
            f"{cfg.promocao_min_projetos} não foi atingido"
        )
    abaixo = [(pid, nota) for pid, nota in por_projeto if nota < cfg.promocao_nota_min]
    if abaixo:
        detalhe = ", ".join(f"{pid} (média {nota:.2f})" for pid, nota in abaixo)
        problemas.append(f"projetos com média abaixo de {cfg.promocao_nota_min:.2f}: {detalhe}")

    if problemas:
        return Elegibilidade(False, total, media, "Não elegível: " + "; ".join(problemas) + ".")
    return Elegibilidade(
        True,
        total,
        media,
        f"Elegível: {total} projetos concluídos (mínimo de {cfg.promocao_min_projetos}), "
        f"todos com média >= {cfg.promocao_nota_min:.2f}.",
    )
