"""Enriquecimento de ideias (processo 1.1), sempre sob demanda do curador (R28).

Não roda dentro de handler de outbox nem sobre as ideias criadas pelo 2.2: só a rota
`POST /acervo/ideias/{id}/enriquecimento` chama `enriquecer`. O agente `enriquecedor` usa saída
estruturada, sem ferramentas web: sugere tags de domínio, fontes e um playbook a partir do que o
modelo sabe, e o curador decide o que fazer com a ideia.

O uso do LLM é gravado em `execucao.chamada_llm` com `ideia_id` (R30): fica registrado e
consultável, mas não entra no orçamento de nenhum projeto.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from pydantic import BaseModel

from ais4.acervo import servico, similaridade
from ais4.acervo.similaridade import Embedder
from ais4.comum.erros import RegraViolada, ServicoIndisponivel
from ais4.config import obter_config
from ais4.execucao import orcamento
from ais4.llm.cliente import ClienteLLM, RespostaInvalida, Uso
from ais4.playbooks import servico as playbooks

log = logging.getLogger(__name__)

PAPEL_ENRIQUECEDOR = "enriquecedor"
STATUS_ENRIQUECIVEIS = ("rascunho", "triagem")

PROMPT_ENRIQUECEDOR = """\
Você enriquece ideias de projetos de pesquisa e dados de um acervo. Não há acesso à web: use só \
o que você sabe e nunca invente.

Para a ideia recebida, sugira:
- `dominio`: tags de domínio curtas, em minúsculas, que faltam na ideia (não repita as existentes);
- `fontes`: fontes de dados públicas que você sabe que existem e que ajudariam a responder a \
pergunta (nome oficial; `url` só se você a conhece com certeza, senão null; `cobertura` curta);
- `playbook_codigo`: o código de UM dos playbooks listados que melhor serve à ideia, ou null se \
nenhum serve;
- `justificativa`: uma frase explicando as sugestões.

Quem decide é o curador. Prefira poucas sugestões boas a muitas incertas.\
"""


class FonteSugerida(BaseModel):
    nome: str
    url: str | None
    cobertura: str


class SugestaoEnriquecimento(BaseModel):
    """Saída estruturada do agente `enriquecedor`."""

    dominio: list[str]
    fontes: list[FonteSugerida]
    playbook_codigo: str | None
    justificativa: str


@dataclass(frozen=True)
class Enriquecimento:
    """O que foi **acrescentado** à ideia (já gravado) e as candidatas de duplicidade."""

    dominio_sugerido: list[str]
    fontes_sugeridas: list[dict]
    playbook_sugerido: str | None
    candidatas_duplicidade: list[tuple[UUID, float]] = field(default_factory=list)


def _registrar_na_ideia(ideia_id: UUID) -> Callable[[Uso], None]:
    """`ao_usar` do enriquecimento: grava a chamada em `execucao.chamada_llm` com `ideia_id`."""

    def registrar(uso: Uso) -> None:
        orcamento.registrar_chamada(
            uso, projeto_id=None, tarefa_id=None, proposta_id=None, ideia_id=ideia_id
        )

    return registrar


def _norm(texto: str | None) -> str:
    return (texto or "").strip().casefold()


def _mensagem(ideia, codigos: list[str]) -> str:
    fontes = "; ".join(f.get("nome", "?") for f in ideia.fontes_candidatas) or "(nenhuma)"
    return "\n".join(
        [
            f"Título: {ideia.titulo}",
            f"Tipo: {ideia.tipo}",
            f"Descrição: {ideia.descricao}",
            f"Pergunta norteadora: {ideia.pergunta_norteadora}",
            f"Domínio atual: {', '.join(ideia.dominio)}",
            f"Fontes já indicadas: {fontes}",
            f"Playbook já indicado: {'sim' if ideia.playbook_sugerido_id else 'não'}",
            f"Códigos de playbook disponíveis: {', '.join(codigos) or '(nenhum)'}",
        ]
    )


def _tags_novas(ideia, sugestao: SugestaoEnriquecimento) -> list[str]:
    vistas = {_norm(t) for t in ideia.dominio}
    novas = []
    for tag in sugestao.dominio:
        if _norm(tag) and _norm(tag) not in vistas:
            vistas.add(_norm(tag))
            novas.append(tag.strip())
    return novas


def _fontes_novas(ideia, sugestao: SugestaoEnriquecimento) -> list[dict]:
    nomes = {_norm(f.get("nome")) for f in ideia.fontes_candidatas}
    urls = {_norm(f.get("url")) for f in ideia.fontes_candidatas} - {""}
    novas = []
    for fonte in sugestao.fontes:
        nome, url = _norm(fonte.nome), _norm(fonte.url)
        if not nome or nome in nomes or (url and url in urls):
            continue
        nomes.add(nome)
        urls.add(url)
        item = {"nome": fonte.nome.strip()}
        if url:
            item["url"] = fonte.url.strip()
        if fonte.cobertura.strip():
            item["cobertura"] = fonte.cobertura.strip()
        novas.append(item)
    return novas


def enriquecer(
    ideia_id: UUID, cliente: ClienteLLM, embedder: Embedder, usuario: str
) -> Enriquecimento:
    """Acrescenta tags e fontes **novas** (nunca remove as do autor) e, se estiver vazio, o playbook.

    A ideia segue `rascunho`/`triagem` (outro status: `RegraViolada`). Depois de gravar, indexa a
    ideia e devolve as candidatas de duplicidade (similaridade acima do limiar configurado).
    """
    ideia = servico.obter(ideia_id)
    if ideia.status not in STATUS_ENRIQUECIVEIS:
        raise RegraViolada(
            f"Só ideias em rascunho ou triagem podem ser enriquecidas (status: {ideia.status})"
        )
    versoes = {v.codigo: v.playbook_id for v in playbooks.versoes_ativas()}
    try:
        sugestao = cliente.estruturado(
            papel=PAPEL_ENRIQUECEDOR,
            sistema=PROMPT_ENRIQUECEDOR,
            mensagem=_mensagem(ideia, sorted(versoes)),
            formato=SugestaoEnriquecimento,
            ao_usar=_registrar_na_ideia(ideia_id),
        )
    except RespostaInvalida as erro:
        raise ServicoIndisponivel(
            f"O enriquecimento da ideia {ideia_id} falhou: resposta inválida do modelo ({erro.motivo})"
        ) from erro

    tags = _tags_novas(ideia, sugestao)
    fontes = _fontes_novas(ideia, sugestao)
    codigo = sugestao.playbook_codigo
    playbook_id = versoes.get(codigo) if codigo and ideia.playbook_sugerido_id is None else None
    campos = {}
    if tags:
        campos["dominio"] = [*ideia.dominio, *tags]
    if fontes:
        campos["fontes_candidatas"] = [*ideia.fontes_candidatas, *fontes]
    if playbook_id is not None:
        campos["playbook_sugerido_id"] = playbook_id
    if campos:
        servico.atualizar(ideia_id, servico.IdeiaAtualizar(**campos), usuario)

    similaridade.indexar(ideia_id, embedder)
    limiar = obter_config().limiar_duplicidade
    candidatas = [
        (outra.id, sim)
        for outra, sim in similaridade.similares(ideia_id, limite=servico.LIMITE_CANDIDATAS)
        if sim >= limiar
    ]
    return Enriquecimento(tags, fontes, codigo if playbook_id is not None else None, candidatas)
