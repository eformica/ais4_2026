"""Ferramentas dos agentes de execução (D8): Python no sandbox, leitura e conclusão de etapa.

Todas as ferramentas locais são `@beta_tool(strict=True)` sem parâmetros de objeto livre
(estrutura vai como JSON em texto). Nenhuma levanta exceção por entrada do modelo: erros viram
texto devolvido ao modelo, para que ele corrija e tente de novo.
"""

import codecs
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from anthropic import beta_tool

from ais4.comum.dto import ArtefatoRef
from ais4.comum.erros import NaoEncontrado
from ais4.execucao.artefatos import Armazem
from ais4.execucao.sandbox import (
    PASTA_SCRIPTS,
    ErroWorkspace,
    ResultadoExec,
    criar_exclusivo,
    e_ligacao,
    garantir_pasta,
    verificar_destino,
)
from ais4.llm.cliente import FERRAMENTA_BUSCA_WEB, FERRAMENTA_LEITURA_WEB
from ais4.playbooks.base import Etapa, SaidaEtapa

ETAPA_COM_REDE = "coleta_dados"
MAX_USES_BUSCA = 20
MAX_USES_LEITURA = 30
LIMITE_LEITURA = 50000  # caracteres devolvidos por `ler_artefato`
LIMITE_SAIDA_MODELO = 5000  # caracteres de stdout/stderr devolvidos por `executar_python`
PASTA_ENTRADAS = "entradas"
PREFIXO_CONTAINER = "/work/"

FERRAMENTAS_DO_SANDBOX = frozenset({"python", "documentos", "slides"})

_SUFIXOS_DATASET = frozenset(
    {".csv", ".tsv", ".parquet", ".xlsx", ".xls", ".json", ".zip", ".geojson", ".gpkg", ".shp"}
)
_SUFIXOS_GRAFICO = frozenset({".png", ".jpg", ".jpeg", ".svg"})
_SUFIXOS_DOCUMENTO = frozenset({".docx", ".pdf", ".md"})


class _ExecutorDeSandbox(Protocol):
    def executar(
        self, workspace: Path, script: str, *, rede: bool, timeout_s: int = 900, semente: int
    ) -> ResultadoExec: ...


@dataclass
class ContextoTarefa:
    """Estado de uma tarefa de execução compartilhado entre as ferramentas.

    `artefatos_disponiveis` (artefatos das etapas ancestrais) é preparado por quem cria o
    contexto, usando `ancestrais`. `scripts` e `execucoes` acumulam o que o orquestrador precisa
    registrar (proveniência); `len(scripts)` é o contador de scripts da tarefa.
    """

    projeto_id: UUID
    tarefa_id: UUID
    etapa: Etapa
    workspace: Path
    sandbox: _ExecutorDeSandbox
    armazem: Armazem
    semente: int
    artefatos_disponiveis: dict[str, ArtefatoRef] = field(default_factory=dict)
    saida: SaidaEtapa | None = None
    scripts: list[ArtefatoRef] = field(default_factory=list)
    execucoes: list[ResultadoExec] = field(default_factory=list)


def ancestrais(etapas: list[Etapa], codigo: str) -> set[str]:
    """Códigos das etapas de que `codigo` depende, direta ou transitivamente."""
    por_codigo = {e.codigo: e for e in etapas}
    if codigo not in por_codigo:
        raise NaoEncontrado(f"Etapa {codigo!r} não existe no playbook")
    visitadas: set[str] = set()
    pendentes = list(por_codigo[codigo].depende_de)
    while pendentes:
        atual = pendentes.pop()
        if atual in visitadas or atual not in por_codigo:
            continue
        visitadas.add(atual)
        pendentes.extend(por_codigo[atual].depende_de)
    visitadas.discard(codigo)
    return visitadas


def _nome_de_entrada_valido(nome: str) -> bool:
    return bool(nome) and nome not in {".", ".."} and "/" not in nome and "\\" not in nome


def materializar_entradas(contexto: ContextoTarefa) -> list[Path]:
    """Copia os artefatos disponíveis para `workspace/entradas/<nome>` (legíveis no sandbox).

    `ValueError` (`ErroWorkspace`) se o workspace tiver um link no caminho de destino: o código do
    agente roda com o workspace montado em escrita e poderia ter plantado um.
    """
    destino = contexto.workspace / PASTA_ENTRADAS
    copiados: list[Path] = []
    for nome, ref in contexto.artefatos_disponiveis.items():
        if not _nome_de_entrada_valido(nome):
            raise ValueError(f"nome de artefato inválido para o workspace: {nome!r}")
        garantir_pasta(contexto.workspace, destino)
        caminho = destino / nome
        verificar_destino(contexto.workspace, caminho)
        if caminho.exists() or e_ligacao(caminho):
            if caminho.is_dir():
                raise ErroWorkspace(f"{PASTA_ENTRADAS}/{nome} é um diretório no workspace")
            caminho.unlink()  # reaproveitamento do workspace: recria sem seguir links
        with (
            contexto.armazem.abrir(ref.uri) as origem,
            criar_exclusivo(contexto.workspace, caminho) as saida,
        ):
            shutil.copyfileobj(origem, saida)
        copiados.append(caminho)
    return copiados


def usa_sandbox(etapa: Etapa) -> bool:
    """A etapa tem ferramenta que roda no sandbox Docker (`executar_python`)."""
    return bool(FERRAMENTAS_DO_SANDBOX & set(etapa.papel_agente.ferramentas))


def tipo_do_artefato(nome: str, caminho: Path) -> str:
    """Tipo de `projetos.artefato` pela extensão do arquivo e pelo nome da saída.

    `.py` código; tabelas e JSON dataset; imagens gráfico; `.pptx` apresentação; `.docx`,
    `.pdf` e `.md` são relatório só quando o nome (da saída ou do arquivo) contém "relatorio".
    """
    sufixo = caminho.suffix.lower()
    if sufixo == ".py":
        return "codigo"
    if sufixo in _SUFIXOS_DATASET:
        return "dataset"
    if sufixo in _SUFIXOS_GRAFICO:
        return "grafico"
    if sufixo == ".pptx":
        return "apresentacao"
    if sufixo in _SUFIXOS_DOCUMENTO and "relatorio" in f"{nome} {caminho.name}".lower():
        return "relatorio"
    return "outro"


def _cauda(texto: str) -> str:
    return texto[-LIMITE_SAIDA_MODELO:]


def _resolver_no_workspace(workspace: Path, informado: str) -> Path:
    """Caminho do modelo (relativo ou `/work/...`) -> absoluto dentro do workspace.

    Não segue links: `ValueError` (`ErroWorkspace`) se o caminho escapar do workspace ou se
    qualquer componente dentro dele for link, junction ou ponto de reanálise (R23). O código do
    agente roda com o workspace em escrita e poderia plantar um link para um arquivo do host.
    """
    bruto = informado.strip()
    if bruto.startswith(PREFIXO_CONTAINER):
        bruto = bruto.removeprefix(PREFIXO_CONTAINER)
    if not bruto:
        raise ValueError("caminho vazio")
    candidato = Path(bruto)
    if candidato.is_absolute() or candidato.drive or candidato.root:
        raise ValueError("caminho absoluto fora do workspace")
    return verificar_destino(workspace, workspace / candidato)


def _ferramenta_executar_python(contexto: ContextoTarefa):
    @beta_tool(strict=True)
    def executar_python(codigo: str) -> str:
        """Executa um script Python no sandbox e devolve código de saída, saída e arquivos novos.

        O diretório de trabalho é /work (o workspace da etapa). Artefatos das etapas anteriores
        estão em /work/entradas/<nome>. Pandas, numpy, statsmodels, scikit-learn, geopandas,
        matplotlib, python-docx, python-pptx e openpyxl estão instalados. Use sementes fixas.

        Args:
            codigo: Código-fonte Python completo do script.
        """
        try:
            resultado = contexto.sandbox.executar(
                contexto.workspace,
                codigo,
                rede=contexto.etapa.codigo == ETAPA_COM_REDE,
                semente=contexto.semente,
            )
        except ErroWorkspace as erro:
            return f"Erro: o script não foi executado. {erro}"
        nome = (
            resultado.script.name if resultado.script else f"_script_{len(contexto.scripts) + 1}.py"
        )
        uri, sha = contexto.armazem.salvar(codigo.encode("utf-8"))
        contexto.scripts.append(ArtefatoRef(uri=uri, sha256=sha, nome=nome, tipo="codigo"))
        contexto.execucoes.append(resultado)

        novos = "\n".join(f"- {p.as_posix()}" for p in resultado.arquivos_novos) or "(nenhum)"
        return (
            f"codigo_saida: {resultado.codigo_saida}\n"
            f"stdout (final):\n{_cauda(resultado.stdout)}\n"
            f"stderr (final):\n{_cauda(resultado.stderr)}\n"
            f"arquivos novos ou alterados:\n{novos}"
        )

    return executar_python


def _ferramenta_ler_artefato(contexto: ContextoTarefa):
    @beta_tool(strict=True)
    def ler_artefato(nome: str) -> str:
        """Lê o conteúdo em texto de um artefato produzido por uma etapa anterior.

        Args:
            nome: Nome do artefato, como listado na especificação das etapas anteriores.
        """
        disponiveis = contexto.artefatos_disponiveis
        ref = disponiveis.get(nome)
        if ref is None:
            lista = ", ".join(sorted(disponiveis)) or "nenhum artefato disponível"
            return f"Artefato {nome!r} não encontrado. Disponíveis: {lista}."
        try:
            with contexto.armazem.abrir(ref.uri) as arquivo:
                dados = arquivo.read(LIMITE_LEITURA * 4 + 4)
                completo = not arquivo.read(1)
        except (OSError, ValueError) as erro:
            return f"Erro: não foi possível abrir o artefato {nome!r} ({erro})."
        decodificador = codecs.getincrementaldecoder("utf-8")()
        try:
            texto = decodificador.decode(dados, final=completo)
        except UnicodeDecodeError:
            texto = "\x00"
        if "\x00" in texto:
            return (
                f"O artefato {nome!r} é binário e não pode ser lido como texto. "
                f"Leia-o com executar_python em /work/{PASTA_ENTRADAS}/{nome}."
            )
        if len(texto) > LIMITE_LEITURA or not completo:
            return (
                f"{texto[:LIMITE_LEITURA]}\n[conteúdo truncado em {LIMITE_LEITURA} caracteres; "
                f"use executar_python em /work/{PASTA_ENTRADAS}/{nome} para ler tudo]"
            )
        return texto

    return ler_artefato


def _ferramenta_concluir_etapa(contexto: ContextoTarefa):
    @beta_tool(strict=True)
    def concluir_etapa(artefatos_json: str, resumo: str) -> str:
        """Entrega as saídas da etapa e a encerra. Chame uma vez, ao final.

        Args:
            artefatos_json: Objeto JSON em texto que mapeia o nome de cada saída esperada para o
                caminho do arquivo no workspace, por exemplo {"nota_de_contexto": "nota.md"}.
                Cada saída é um único arquivo (agrupe várias em um .zip).
            resumo: Resumo do que foi feito, com limitações e pendências.
        """
        try:
            pedido = json.loads(artefatos_json)
        except (ValueError, RecursionError) as erro:
            return f"Erro: artefatos_json não é JSON válido ({erro})."
        if not isinstance(pedido, dict) or not all(isinstance(v, str) for v in pedido.values()):
            return "Erro: artefatos_json deve ser um objeto JSON {nome: caminho_no_workspace}."

        try:
            erros: list[str] = []
            arquivos: dict[str, Path] = {}
            for nome, informado in pedido.items():
                if not _nome_de_entrada_valido(nome):
                    erros.append(f"nome de saída inválido: {nome!r}")
                    continue
                try:
                    caminho = _resolver_no_workspace(contexto.workspace, informado)
                except ValueError as erro:
                    erros.append(f"{nome}: {informado!r} ({erro})")
                    continue
                if caminho.is_dir():
                    erros.append(
                        f"{nome}: {informado!r} é um diretório; empacote-o em um .zip e informe o .zip"
                    )
                elif not caminho.is_file():
                    erros.append(f"{nome}: arquivo {informado!r} não existe no workspace")
                else:
                    arquivos[nome] = caminho
            if erros:
                return "Erro: nada foi registrado. " + "; ".join(erros) + "."

            refs: dict[str, ArtefatoRef] = {}
            for nome, caminho in arquivos.items():
                uri, sha = contexto.armazem.salvar(verificar_destino(contexto.workspace, caminho))
                refs[nome] = ArtefatoRef(
                    uri=uri, sha256=sha, nome=nome, tipo=tipo_do_artefato(nome, caminho)
                )
            contexto.saida = SaidaEtapa(artefatos=refs, resumo=resumo)

            mensagem = f"Etapa concluída: {len(refs)} artefato(s) registrado(s): {', '.join(refs)}."
            ausentes = [s for s in contexto.etapa.saidas if s not in refs]
            if ausentes:
                mensagem += f" Saídas esperadas ausentes: {', '.join(ausentes)}."
            return mensagem
        except (OSError, RecursionError) as erro:
            return f"Erro: não foi possível ler os arquivos do workspace ({erro})."

    return concluir_etapa


def _ferramenta_gravar_arquivo(contexto: ContextoTarefa):
    @beta_tool(strict=True)
    def gravar_arquivo(caminho: str, conteudo: str) -> str:
        """Grava (ou substitui) um arquivo de texto UTF-8 no workspace da etapa.

        Use para produzir as saídas da etapa (notas, relatórios em Markdown, JSON) e então
        informe o caminho a `concluir_etapa`.

        Args:
            caminho: Caminho relativo ao workspace (ou /work/...), por exemplo "nota.md".
            conteudo: Conteúdo completo do arquivo.
        """
        try:
            alvo = _resolver_no_workspace(contexto.workspace, caminho)
            relativo = alvo.relative_to(Path(os.path.abspath(contexto.workspace)))
            if not relativo.parts:
                return f"Erro: {caminho!r} não é um caminho de arquivo."
            if relativo.parts[0] == PASTA_SCRIPTS:
                return f"Erro: {PASTA_SCRIPTS}/ é reservada aos scripts executados."
            garantir_pasta(contexto.workspace, alvo.parent)
            if alvo.is_dir():
                return f"Erro: {caminho!r} é um diretório."
            if alvo.exists():
                alvo.unlink()  # sem seguir links: verificar_destino já recusou qualquer link
            with criar_exclusivo(contexto.workspace, alvo) as arquivo:
                arquivo.write(conteudo.encode("utf-8"))
        except (OSError, ValueError) as erro:
            return f"Erro: o arquivo não foi gravado ({erro})."
        return f"Arquivo gravado: {relativo.as_posix()} ({len(conteudo)} caracteres)."

    return gravar_arquivo


def montar_ferramentas(contexto: ContextoTarefa) -> list:
    """Lista para o tool runner: servidor (busca/leitura web), `executar_python` e as básicas.

    Toda etapa recebe `gravar_arquivo`, `ler_artefato` e `concluir_etapa`: sem `gravar_arquivo`,
    as etapas sem Python (contexto, síntese, retroalimentação) não teriam como produzir as saídas
    que `concluir_etapa` exige.
    """
    ferramentas: list = []
    for nome in contexto.etapa.papel_agente.ferramentas:
        if nome == "web_search":
            ferramentas.append({**FERRAMENTA_BUSCA_WEB, "max_uses": MAX_USES_BUSCA})
        elif nome == "web_fetch":
            ferramentas.append({**FERRAMENTA_LEITURA_WEB, "max_uses": MAX_USES_LEITURA})
        elif nome in FERRAMENTAS_DO_SANDBOX:
            if not any(
                not isinstance(f, dict) and f.name == "executar_python" for f in ferramentas
            ):
                ferramentas.append(_ferramenta_executar_python(contexto))
        else:
            raise ValueError(f"Ferramenta desconhecida na etapa {contexto.etapa.codigo}: {nome!r}")
    ferramentas.append(_ferramenta_gravar_arquivo(contexto))
    ferramentas.append(_ferramenta_ler_artefato(contexto))
    ferramentas.append(_ferramenta_concluir_etapa(contexto))
    return ferramentas
