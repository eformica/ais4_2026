"""Agente revisor (spec 7): `validar_etapa` do playbook e, se passar, o parecer de um LLM revisor.

Quem executou não aprova o próprio trabalho: o revisor é outro papel (`revisor`), sem
ferramentas, que só lê o critério de saída, os critérios de qualidade, o resumo do executor e os
artefatos de texto da etapa.
"""

import codecs
from collections.abc import Callable
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, create_model

from ais4.comum.dto import ArtefatoRef
from ais4.execucao.artefatos import Armazem
from ais4.llm.cliente import ClienteLLM, RespostaInvalida, Uso
from ais4.playbooks.base import Criterio, Etapa, Playbook, Resultado, SaidaEtapa

PAPEL_REVISOR = "revisor"
LIMITE_TEXTO = 50000  # caracteres de artefatos de texto enviados ao revisor, somados

PROMPT_REVISOR = """\
Você é o revisor de qualidade de um projeto de pesquisa com dados públicos. Você não executou a \
etapa e não a defende: decide se ela pode liberar as etapas seguintes.

Avalie a entrega contra o critério de saída da etapa e contra cada critério de qualidade do \
playbook. Para cada critério, informe em `por_criterio` se a entrega o atende (true) ou não \
(false); critério que não se aplica à etapa conta como atendido. `aprovado` só é true se o \
critério de saída foi cumprido e nenhum critério falhou.

Em `feedback`, escreva instruções concretas e verificáveis para a próxima tentativa (o que \
falta, onde, como corrigir). Não invente problemas; não aprove o que não pode verificar nos \
artefatos apresentados.
"""


class ParecerRevisor(BaseModel):
    aprovado: bool
    feedback: list[str]
    por_criterio: dict[str, bool]


@lru_cache
def formato_do_parecer(codigos: tuple[str, ...]) -> type[BaseModel]:
    """Schema estrito pedido à API: um campo booleano obrigatório por critério.

    Um `dict[str, bool]` livre vira, na saída estruturada estrita, um objeto sem propriedades
    (o modelo só poderia responder `{}`), por isso o formato é montado com os códigos.
    """
    por_criterio = create_model(
        "PorCriterio",
        __config__=ConfigDict(extra="forbid"),
        **{codigo: (bool, ...) for codigo in codigos},
    )
    return create_model(
        "ParecerRevisorEstrito",
        __config__=ConfigDict(extra="forbid"),
        aprovado=(bool, ...),
        feedback=(list[str], ...),
        por_criterio=(por_criterio, ...),
    )


def _ler_texto(armazem: Armazem, ref: ArtefatoRef, limite: int) -> tuple[str | None, bool]:
    """Até `limite` caracteres do artefato. `(None, _)` se for binário; o bool diz se truncou."""
    with armazem.abrir(ref.uri) as arquivo:
        dados = arquivo.read(limite * 4 + 4)
        completo = not arquivo.read(1)
    try:
        texto = codecs.getincrementaldecoder("utf-8")().decode(dados, final=completo)
    except UnicodeDecodeError:
        return None, False
    if "\x00" in texto:
        return None, False
    return texto[:limite], (len(texto) > limite or not completo)


def _artefatos_em_texto(saida: SaidaEtapa, armazem: Armazem) -> str:
    restante = LIMITE_TEXTO
    blocos = []
    for nome, ref in saida.artefatos.items():
        cabecalho = f"### `{nome}` ({ref.tipo})"
        if restante <= 0:
            blocos.append(f"{cabecalho}\n(omitido: limite de {LIMITE_TEXTO} caracteres atingido)")
            continue
        try:
            texto, truncado = _ler_texto(armazem, ref, restante)
        except (OSError, ValueError) as erro:
            blocos.append(f"{cabecalho}\n(ilegível no armazém: {erro})")
            continue
        if texto is None:
            blocos.append(f"{cabecalho}\n(arquivo binário: conteúdo não incluído)")
            continue
        restante -= len(texto)
        nota = f"\n[truncado: limite de {LIMITE_TEXTO} caracteres de texto]" if truncado else ""
        blocos.append(f"{cabecalho}\n```\n{texto}\n```{nota}")
    return "\n\n".join(blocos) or "(nenhum artefato)"


def _mensagem(etapa: Etapa, saida: SaidaEtapa, criterios: list[Criterio], armazem: Armazem) -> str:
    return "\n\n".join(
        [
            f"# Etapa {etapa.ordem}: {etapa.nome} (`{etapa.codigo}`)",
            f"## Critério de saída\n{etapa.criterio_saida}",
            "## Saídas esperadas\n" + "\n".join(f"- `{s}`" for s in etapa.saidas),
            "## Critérios de qualidade (avalie cada um em `por_criterio`)\n"
            + "\n".join(f"- `{c.codigo}`: {c.descricao}" for c in criterios),
            f"## Resumo do executor\n{saida.resumo}",
            "## Artefatos entregues\n" + _artefatos_em_texto(saida, armazem),
        ]
    )


def revisar(
    playbook: Playbook,
    etapa: Etapa,
    saida: SaidaEtapa,
    criterios: list[Criterio],
    cliente: ClienteLLM,
    ao_usar: Callable[[Uso], None],
    *,
    armazem: Armazem | None = None,
) -> Resultado:
    """Validação determinística do playbook; se aprovada, o parecer do LLM revisor.

    Reprova quando o revisor reprova ou marca algum critério como não atendido. Uma
    `RespostaInvalida` do revisor (recusa, corte, JSON fora do schema) conta como reprovação
    com o feedback `"revisão indisponível: <motivo>"`. `OrcamentoExcedido` (de `ao_usar`) sobe.
    """
    deterministico = playbook.validar_etapa(etapa, saida)
    if not deterministico.aprovado:
        return deterministico
    armazem = armazem or Armazem()
    try:
        bruto = cliente.estruturado(
            papel=PAPEL_REVISOR,
            sistema=PROMPT_REVISOR,
            mensagem=_mensagem(etapa, saida, criterios, armazem),
            formato=formato_do_parecer(tuple(c.codigo for c in criterios)),
            ao_usar=ao_usar,
        )
    except RespostaInvalida as erro:
        return Resultado(aprovado=False, feedback=[f"revisão indisponível: {erro}"])
    parecer = ParecerRevisor(
        aprovado=bruto.aprovado,
        feedback=[f.strip() for f in bruto.feedback if f.strip()],
        por_criterio=bruto.por_criterio.model_dump(),
    )
    falhos = [c for c in criterios if not parecer.por_criterio.get(c.codigo, False)]
    if parecer.aprovado and not falhos:
        return Resultado(aprovado=True, feedback=parecer.feedback)
    feedback = parecer.feedback + [
        f"Critério não atendido: {c.codigo} ({c.descricao})." for c in falhos
    ]
    return Resultado(
        aprovado=False, feedback=feedback or ["O revisor reprovou a etapa sem detalhar o motivo."]
    )
