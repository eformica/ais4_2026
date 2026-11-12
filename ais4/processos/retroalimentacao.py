"""Processo 2.2: retroalimentação do projeto em revisão (A.4: `ProjetoStatusAlterado` -> em_revisao).

Lê os artefatos `novas_ideias` e `licoes_aprendidas` da última tentativa `concluida` da etapa
`retroalimentacao` (formato em `ais4.comum.retroalimentacao`, cobrado por `validar_etapa`) e grava:

- um `proximo_passo` por item de `novas_ideias`; `acao == "ideia"` cria a ideia derivada
  (`rascunho`, origem `retroalimentacao`) e a vincula ao passo; `descartar` descarta o passo;
- uma `licao_aprendida` por item de `licoes_aprendidas`; as de tipo `falhou`/`sugestao` viram
  proposta de melhoria do playbook do projeto e são marcadas como encaminhadas.

Idempotência (D4): todos os ids são determinísticos (`uuid5`) e as inserções usam ON CONFLICT DO
NOTHING, então reprocessar o evento, inclusive depois de uma queda entre "criar a ideia" e
"vincular ao passo", não duplica nada. Nenhuma chamada de LLM acontece aqui (R28).

Artefato ausente, ilegível ou fora do formato (não deveria ocorrer depois da validação da etapa):
registra `logger.error` e o evento é dado como tratado. O projeto simplesmente não consegue ser
concluído até um humano corrigir, porque o gatilho do banco exige lições e próximos passos
resolvidos.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from uuid import UUID, uuid5

from ais4.acervo import servico as acervo
from ais4.acervo.servico import IdeiaCriar
from ais4.comum import eventos
from ais4.comum.dto import ProjetoDTO
from ais4.comum.erros import NaoEncontrado
from ais4.comum.eventos import Evento
from ais4.comum.retroalimentacao import (
    LicaoRetro,
    NovaIdeiaRetro,
    ler_licoes,
    ler_novas_ideias,
)
from ais4.execucao import servico as execucao
from ais4.execucao.artefatos import Armazem
from ais4.playbooks import registro
from ais4.playbooks import servico as playbooks
from ais4.projetos import servico as projetos

log = logging.getLogger(__name__)

NAMESPACE_IDEIAS = UUID("6f1c2a4e-0000-4000-8000-a15400000001")
USUARIO = "sistema:retroalimentacao"
ETAPA = "retroalimentacao"


def id_ideia_derivada(proximo_passo_id: UUID) -> UUID:
    return uuid5(NAMESPACE_IDEIAS, str(proximo_passo_id))


def _id_proximo_passo(projeto_id: UUID, i: int, texto: str) -> UUID:
    return uuid5(NAMESPACE_IDEIAS, f"pp:{projeto_id}:{i}:{texto}")


def _id_licao(projeto_id: UUID, j: int, descricao: str) -> UUID:
    return uuid5(NAMESPACE_IDEIAS, f"licao:{projeto_id}:{j}:{descricao}")


def _ler_artefato(tarefa, nome: str, armazem: Armazem) -> object | None:
    """O JSON do artefato de saída da tentativa, ou `None` (com `logger.error`) se não der."""
    ref = (tarefa.saidas or {}).get(nome)
    if not isinstance(ref, dict) or "uri" not in ref or "sha256" not in ref:
        log.error(
            "Projeto %s: a tentativa %s não entregou o artefato %s",
            tarefa.projeto_id,
            tarefa.tentativa,
            nome,
        )
        return None
    try:
        if not armazem.verificar(ref["uri"], ref["sha256"]):
            raise ValueError("conteúdo diverge do SHA-256 registrado")
        with armazem.abrir(ref["uri"]) as arquivo:
            return json.load(arquivo)
    except (OSError, ValueError) as erro:  # JSON/UTF-8 inválido, fora do armazém, sumiu, adulterado
        log.error(
            "Projeto %s: não foi possível ler o artefato %s (%s)", tarefa.projeto_id, nome, erro
        )
        return None


def _ultima_tentativa_concluida(projeto_id: UUID):
    concluidas = [
        t
        for t in execucao.tarefas(projeto_id)
        if t.etapa_codigo == ETAPA and t.status == "concluida"
    ]
    return max(concluidas, key=lambda t: t.tentativa, default=None)


def _fallback(projeto: ProjetoDTO, passo: str) -> IdeiaCriar | None:
    """Ideia do `Playbook.retroalimentar` para um item sem objeto `ideia` utilizável."""
    try:
        versao = next(
            v
            for v in playbooks.listar_versoes()
            if v.playbook_id == projeto.playbook_id and v.versao == projeto.playbook_versao
        )
        playbook = registro.obter(versao.codigo, versao.versao)
        [nova] = playbook.retroalimentar([passo], acervo.obter(projeto.ideia_origem_id))
    except (StopIteration, NaoEncontrado, ValueError):
        return None
    return IdeiaCriar(**asdict(nova), status="rascunho")


def _ideia_do_item(projeto: ProjetoDTO, item: NovaIdeiaRetro) -> IdeiaCriar | None:
    if item.ideia is None:
        return _fallback(projeto, item.proximo_passo)
    dados = item.ideia.model_dump()
    dados["titulo"] = dados["titulo"].strip()
    return IdeiaCriar(**dados, status="rascunho", fontes_candidatas=[])


def _processar_novas_ideias(projeto: ProjetoDTO, itens: list[NovaIdeiaRetro]) -> None:
    existentes = {p.id: p for p in projetos.proximos_passos(projeto.id)}
    for i, item in enumerate(itens):
        passo_id = _id_proximo_passo(projeto.id, i, item.proximo_passo)
        projetos.registrar_proximo_passo(projeto.id, item.proximo_passo, id=passo_id)
        atual = existentes.get(passo_id)
        if atual is not None and (atual.ideia_gerada_id or atual.descartado_motivo):
            continue  # já resolvido numa passagem anterior
        if item.acao == "descartar":
            projetos.descartar_proximo_passo(passo_id, item.motivo_descarte or "", USUARIO)
            continue
        dados = _ideia_do_item(projeto, item)
        if dados is None:
            log.error(
                "Projeto %s: não foi possível derivar uma ideia do próximo passo %r",
                projeto.id,
                item.proximo_passo,
            )
            continue
        ideia = acervo.criar_ideia_derivada(id_ideia_derivada(passo_id), dados, USUARIO)
        projetos.vincular_ideia_gerada(passo_id, ideia.id)


def _processar_licoes(projeto: ProjetoDTO, itens: list[LicaoRetro]) -> None:
    existentes = {li.id: li for li in projetos.licoes(projeto.id)}
    for j, item in enumerate(itens):
        licao_id = _id_licao(projeto.id, j, item.descricao)
        descricao = f"{item.descricao}\n\nEvidência: {item.evidencia}"
        projetos.registrar_licao(projeto.id, item.tipo, descricao, projeto.playbook_id, id=licao_id)
        if item.tipo == "funcionou":
            continue  # nada a propor ao playbook
        atual = existentes.get(licao_id)
        if atual is not None and atual.encaminhada_em is not None:
            continue
        playbooks.registrar_proposta_melhoria(
            projeto.playbook_id, projeto.playbook_versao, descricao, licao_id, projeto.id, USUARIO
        )
        projetos.marcar_licao_encaminhada(licao_id)


def processar_em_revisao(evento: Evento, armazem: Armazem | None = None) -> None:
    """Handler de `ProjetoStatusAlterado` -> `em_revisao` (`evento.agregado_id` = projeto)."""
    projeto = projetos.obter(evento.agregado_id)
    tentativa = _ultima_tentativa_concluida(projeto.id)
    if tentativa is None:
        log.error(
            "Projeto %s em revisão sem tentativa concluída da etapa %s; nada foi registrado",
            projeto.id,
            ETAPA,
        )
        return
    armazem = armazem or Armazem()
    novas = _ler_artefato(tentativa, "novas_ideias", armazem)
    licoes = _ler_artefato(tentativa, "licoes_aprendidas", armazem)
    for nome, dados, ler, processar in (
        (
            "novas_ideias",
            novas,
            lambda d: ler_novas_ideias(d, tolerante=True),
            _processar_novas_ideias,
        ),
        ("licoes_aprendidas", licoes, ler_licoes, _processar_licoes),
    ):
        if dados is None:
            continue
        itens, erros = ler(dados)
        if erros:
            log.error("Projeto %s: %s fora do formato: %s", projeto.id, nome, " ".join(erros))
            continue
        processar(projeto, itens)


def _para(valor: str) -> Callable[[Evento], bool]:
    return lambda e: e.payload.get("para") == valor


def registrar() -> None:
    """Assina o handler no publicador (idempotente)."""
    eventos.assinar("projetos", "ProjetoStatusAlterado", processar_em_revisao, _para("em_revisao"))
