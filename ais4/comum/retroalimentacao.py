"""Formato dos artefatos da etapa `retroalimentacao` (processo 2.2).

Fonte única do contrato: o playbook o cobra em `validar_etapa` (a tentativa malformada é reprovada
durante a execução, com feedback em português) e o processo 2.2 o lê para gravar próximos passos,
ideias derivadas e lições.

`novas_ideias`: lista JSON, ao menos 1 item, cada um
  {"proximo_passo": str, "acao": "ideia" | "descartar", "motivo_descarte": str (se descartar),
   "ideia": {"titulo": str <= 100, "tipo": <tipo de ideia>, "descricao": str,
             "pergunta_norteadora": str, "dominio": [str, ...] (>= 1),
             "valor": 1..5, "viabilidade": 1..5} (se acao == "ideia")}
`licoes_aprendidas`: lista JSON, ao menos 1 item, cada um
  {"tipo": "funcionou" | "falhou" | "sugestao", "descricao": str, "evidencia": str}
"""

from pydantic import BaseModel

TIPOS_IDEIA = ("problema", "oportunidade", "hipotese", "pergunta_de_pesquisa")
TIPOS_LICAO = ("funcionou", "falhou", "sugestao")
ACOES = ("ideia", "descartar")
TITULO_MAX = 100


class IdeiaProposta(BaseModel):
    titulo: str
    tipo: str
    descricao: str
    pergunta_norteadora: str
    dominio: list[str]
    valor: int
    viabilidade: int


class NovaIdeiaRetro(BaseModel):
    proximo_passo: str
    acao: str
    motivo_descarte: str | None = None
    ideia: IdeiaProposta | None = None


class LicaoRetro(BaseModel):
    tipo: str
    descricao: str
    evidencia: str


def _texto(valor: object) -> bool:
    return isinstance(valor, str) and bool(valor.strip())


def _nota(valor: object) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool) and 1 <= valor <= 5


def _ler_ideia(obj: object, onde: str) -> tuple[IdeiaProposta | None, list[str]]:
    if not isinstance(obj, dict):
        return None, [f"{onde}: 'ideia' deve ser um objeto JSON quando a acao é 'ideia'."]
    erros = []
    for campo in ("titulo", "descricao", "pergunta_norteadora"):
        if not _texto(obj.get(campo)):
            erros.append(f"{onde}: a ideia não informa '{campo}'.")
    if _texto(obj.get("titulo")) and len(obj["titulo"].strip()) > TITULO_MAX:
        erros.append(f"{onde}: o 'titulo' da ideia passa de {TITULO_MAX} caracteres.")
    if obj.get("tipo") not in TIPOS_IDEIA:
        erros.append(f"{onde}: o 'tipo' da ideia deve ser um de {', '.join(TIPOS_IDEIA)}.")
    dominio = obj.get("dominio")
    if not (isinstance(dominio, list) and dominio and all(_texto(d) for d in dominio)):
        erros.append(f"{onde}: 'dominio' deve ser uma lista com ao menos um texto.")
    for campo in ("valor", "viabilidade"):
        if not _nota(obj.get(campo)):
            erros.append(f"{onde}: '{campo}' deve ser um inteiro de 1 a 5.")
    if erros:
        return None, erros
    return IdeiaProposta.model_validate(obj), []


def ler_novas_ideias(
    dados: object, *, tolerante: bool = False
) -> tuple[list[NovaIdeiaRetro], list[str]]:
    """Valida `novas_ideias`. Devolve `(itens, [])` ou `([], mensagens)` (todas, em português).

    `tolerante`: uma `ideia` ausente/inválida numa acao "ideia" não é erro; o item sai com
    `ideia=None` (o processo 2.2 então recorre ao `Playbook.retroalimentar`).
    """
    if not isinstance(dados, list):
        return [], ["novas_ideias deve ser uma lista JSON com um objeto por próximo passo."]
    if not dados:
        return [], ["novas_ideias está vazio: trate ao menos um próximo passo."]
    itens: list[NovaIdeiaRetro] = []
    erros: list[str] = []
    for i, item in enumerate(dados, start=1):
        onde = f"novas_ideias: item {i}"
        if not isinstance(item, dict):
            erros.append(f"{onde} não é um objeto JSON.")
            continue
        passo, acao = item.get("proximo_passo"), item.get("acao")
        antes = len(erros)
        if not _texto(passo):
            erros.append(f"{onde} não informa 'proximo_passo'.")
        if acao not in ACOES:
            erros.append(f"{onde}: 'acao' deve ser 'ideia' ou 'descartar'.")
        ideia = None
        if acao == "descartar" and not _texto(item.get("motivo_descarte")):
            erros.append(f"{onde}: descartar exige 'motivo_descarte'.")
        if acao == "ideia":
            ideia, erros_ideia = _ler_ideia(item.get("ideia"), onde)
            if not tolerante:
                erros += erros_ideia
        if len(erros) == antes:
            itens.append(
                NovaIdeiaRetro(
                    proximo_passo=passo.strip(),
                    acao=acao,
                    motivo_descarte=(item.get("motivo_descarte") or "").strip() or None,
                    ideia=ideia,
                )
            )
    return ([], erros) if erros else (itens, [])


def ler_licoes(dados: object) -> tuple[list[LicaoRetro], list[str]]:
    """Valida `licoes_aprendidas`. Devolve `(itens, [])` ou `([], mensagens)`."""
    if not isinstance(dados, list):
        return [], ["licoes_aprendidas deve ser uma lista JSON com um objeto por lição."]
    if not dados:
        return [], ["licoes_aprendidas está vazio: registre ao menos uma lição."]
    itens: list[LicaoRetro] = []
    erros: list[str] = []
    for i, item in enumerate(dados, start=1):
        onde = f"licoes_aprendidas: item {i}"
        if not isinstance(item, dict):
            erros.append(f"{onde} não é um objeto JSON.")
            continue
        antes = len(erros)
        if item.get("tipo") not in TIPOS_LICAO:
            erros.append(f"{onde}: 'tipo' deve ser um de {', '.join(TIPOS_LICAO)}.")
        for campo in ("descricao", "evidencia"):
            if not _texto(item.get(campo)):
                erros.append(f"{onde} não informa '{campo}'.")
        if len(erros) == antes:
            itens.append(
                LicaoRetro(
                    tipo=item["tipo"],
                    descricao=item["descricao"].strip(),
                    evidencia=item["evidencia"].strip(),
                )
            )
    return ([], erros) if erros else (itens, [])
