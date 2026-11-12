"""Peças de uma tentativa de etapa: semente, workspace, entradas, prompt e registro das saídas.

O orquestrador (`orquestrador.py`) decide quando cada peça entra; aqui fica o que é montado a
partir do estado (projeto, especificação, artefatos) e o que é gravado depois da aprovação.
"""

import hashlib
import json
from pathlib import Path
from uuid import UUID

from ais4.comum.dto import ArtefatoRef, EspecificacaoDTO, TarefaDTO
from ais4.config import obter_config
from ais4.execucao.ferramentas import PASTA_ENTRADAS, PREFIXO_CONTAINER, ContextoTarefa, ancestrais
from ais4.execucao.sandbox import ResultadoExec
from ais4.playbooks.base import Etapa
from ais4.projetos import servico as projetos

ORIGEM_SAIDA = "saida"  # artefato entregue em `concluir_etapa`
ORIGEM_SCRIPT = "script"  # script executado no sandbox (rastreabilidade do código)
SEMENTE_MAXIMA = 2**31 - 1  # a coluna `execucao.tarefa.semente` é `int`


def semente(projeto_id: UUID, etapa_codigo: str, tentativa: int) -> int:
    """Semente determinística da tentativa: os mesmos dados dão a mesma semente."""
    resumo = hashlib.sha256(f"{projeto_id}:{etapa_codigo}:{tentativa}".encode()).digest()
    return int.from_bytes(resumo[:4], "big") & SEMENTE_MAXIMA


def caminho_do_workspace(tarefa: TarefaDTO) -> Path:
    """`<workspaces_dir>/<projeto_id>/<etapa_codigo>/<tentativa>`: um diretório por tentativa."""
    raiz = Path(obter_config().workspaces_dir)
    return raiz / str(tarefa.projeto_id) / tarefa.etapa_codigo / str(tarefa.tentativa)


def artefatos_disponiveis(
    projeto_id: UUID, etapas: list[Etapa], etapa_codigo: str, concluidas: set[UUID]
) -> dict[str, dict]:
    """O artefato mais recente de cada nome entregue pelas etapas ancestrais.

    Só contam saídas (não scripts) de tentativas `concluida`: uma tentativa que registrou
    artefatos e caiu antes de concluir não vaza para as dependentes.
    """
    origem = ancestrais(etapas, etapa_codigo)
    validas = {str(t) for t in concluidas}
    disponiveis: dict[str, dict] = {}
    for artefato in projetos.artefatos(projeto_id):  # em ordem de criação: o último vence
        prov = artefato.proveniencia
        if (
            prov.get("origem") == ORIGEM_SAIDA
            and prov.get("etapa") in origem
            and prov.get("tarefa_id") in validas
        ):
            disponiveis[artefato.nome] = {
                "uri": artefato.uri,
                "sha256": artefato.hash_sha256,
                "tipo": artefato.tipo,
            }
    return disponiveis


def refs_das_entradas(entradas: dict) -> dict[str, ArtefatoRef]:
    return {
        nome: ArtefatoRef(uri=a["uri"], sha256=a["sha256"], nome=nome, tipo=a.get("tipo", "outro"))
        for nome, a in (entradas.get("artefatos") or {}).items()
    }


def itens_de_feedback(feedback: dict | list | str | None) -> list[str]:
    if not feedback:
        return []
    if isinstance(feedback, str):
        return [feedback]
    if isinstance(feedback, dict):
        return [f"{chave}: {valor}" for chave, valor in feedback.items()]
    return [str(item) for item in feedback]


def mensagem_do_executor(
    etapa: Etapa,
    espec: EspecificacaoDTO,
    entradas: dict[str, ArtefatoRef],
    feedback: list[str],
) -> str:
    """Mensagem do usuário para o agente executor; o sistema é o `prompt_base` da etapa."""
    partes = [f"# Etapa {etapa.ordem}: {etapa.nome} (`{etapa.codigo}`)"]
    if etapa.descricao:
        partes.append(etapa.descricao)
    partes.append(f"## Critério de saída\n{etapa.criterio_saida}")
    partes.append(
        "## Saídas esperadas\nGrave cada uma como um arquivo no workspace e entregue todas numa "
        "única chamada a `concluir_etapa`, com exatamente estes nomes:\n"
        + "\n".join(f"- `{s}`" for s in etapa.saidas)
    )
    conteudo = json.dumps(espec.especificacao.model_dump(mode="json"), ensure_ascii=False, indent=2)
    partes.append(f"## Especificação do projeto (versão {espec.versao})\n```json\n{conteudo}\n```")
    if entradas:
        linhas = [
            f"- `{nome}` ({ref.tipo}): {PREFIXO_CONTAINER}{PASTA_ENTRADAS}/{nome}"
            for nome, ref in entradas.items()
        ]
        partes.append(
            "## Artefatos das etapas anteriores\nCópias somente para leitura no workspace; leia "
            "texto com `ler_artefato` e o resto com `executar_python`, se disponível:\n"
            + "\n".join(linhas)
        )
    else:
        partes.append("## Artefatos das etapas anteriores\n(nenhum)")
    if feedback:
        partes.append(
            "## Feedback da tentativa anterior\nCorrija todos os pontos abaixo:\n"
            + "\n".join(f"- {item}" for item in feedback)
        )
    partes.append(
        "## Como terminar\nGrave os arquivos no workspace (`/work`) e termine chamando "
        "`concluir_etapa` com o mapa `{nome: caminho}` e um resumo com limitações e pendências. "
        "Sem essa chamada a etapa não conta como entregue."
    )
    return "\n\n".join(partes)


def ambiente_da_execucao(contexto: ContextoTarefa) -> dict:
    """Ambiente registrado na tarefa: o da última execução no sandbox, com a semente da tarefa."""
    ultima = contexto.execucoes[-1].ambiente if contexto.execucoes else {}
    return {
        "imagem": None,
        "imagem_digest": None,
        "rede": None,
        "seguranca": None,
        **ultima,
        "semente": contexto.semente,
    }


def saidas_da_tarefa(contexto: ContextoTarefa) -> dict[str, dict]:
    if contexto.saida is None:
        return {}
    return {
        nome: {"uri": ref.uri, "sha256": ref.sha256}
        for nome, ref in contexto.saida.artefatos.items()
    }


def _proveniencia(
    tarefa: TarefaDTO, contexto: ContextoTarefa, execucao: ResultadoExec | None, origem: str
) -> dict:
    ambiente = execucao.ambiente if execucao is not None else {}
    return {
        "tarefa_id": str(tarefa.id),
        "etapa": tarefa.etapa_codigo,
        "tentativa": tarefa.tentativa,
        "modelo": tarefa.modelo,
        "imagem_digest": ambiente.get("imagem_digest"),
        "semente": contexto.semente,
        "seguranca": ambiente.get("seguranca"),
        "origem": origem,
    }


def registrar_artefatos(tarefa: TarefaDTO, contexto: ContextoTarefa, marco_id: UUID | None) -> None:
    """Grava em `projetos.artefato` cada saída entregue e cada script executado."""
    ultima = contexto.execucoes[-1] if contexto.execucoes else None
    if contexto.saida is not None:
        for nome, ref in contexto.saida.artefatos.items():
            projetos.registrar_artefato(
                tarefa.projeto_id,
                marco_id,
                ref.tipo,
                nome,
                ref,
                _proveniencia(tarefa, contexto, ultima, ORIGEM_SAIDA),
            )
    for ref, execucao in zip(contexto.scripts, contexto.execucoes, strict=False):
        projetos.registrar_artefato(
            tarefa.projeto_id,
            marco_id,
            "codigo",
            ref.nome,
            ref,
            _proveniencia(tarefa, contexto, execucao, ORIGEM_SCRIPT),
        )
