import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from ais4.comum.dto import ArtefatoRef
from ais4.execucao.artefatos import Armazem
from ais4.execucao.ferramentas import (
    ContextoTarefa,
    ancestrais,
    materializar_entradas,
    montar_ferramentas,
    tipo_do_artefato,
)
from ais4.execucao.sandbox import ResultadoExec
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from ais4.playbooks.base import Etapa

ETAPAS: list[Etapa] = list(ProjetoAcademicoSimples.etapas)
POR_CODIGO = {e.codigo: e for e in ETAPAS}


class SandboxFalso:
    """Registra as chamadas; devolve um resultado fixo e cria arquivos pedidos no workspace."""

    def __init__(self, *, stdout="ola\n", stderr="", codigo=0, novos=("saida.csv",)):
        self.chamadas: list[dict] = []
        self.stdout, self.stderr, self.codigo, self.novos = stdout, stderr, codigo, novos

    def executar(self, workspace, script, *, rede, timeout_s=900, semente):
        self.chamadas.append({"script": script, "rede": rede, "semente": semente})
        pasta = Path(workspace) / "_scripts"
        pasta.mkdir(parents=True, exist_ok=True)
        caminho = pasta / f"_script_{len(self.chamadas)}.py"
        caminho.write_text(script, encoding="utf-8")
        return ResultadoExec(
            stdout=self.stdout,
            stderr=self.stderr,
            codigo_saida=self.codigo,
            arquivos_novos=[Path(n) for n in self.novos],
            ambiente={
                "imagem": "ais4-sandbox:0.1.0",
                "imagem_digest": "sha256:x",
                "semente": semente,
                "rede": rede,
            },
            script=caminho,
        )


def _contexto(tmp_path, codigo_etapa="harmonizacao", **kw) -> ContextoTarefa:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    campos = {
        "projeto_id": uuid4(),
        "tarefa_id": uuid4(),
        "etapa": POR_CODIGO[codigo_etapa],
        "workspace": workspace,
        "sandbox": SandboxFalso(),
        "armazem": Armazem(tmp_path / "armazem"),
        "semente": 42,
    }
    campos.update(kw)
    return ContextoTarefa(**campos)


def _nomes(ferramentas) -> list[str]:
    return [f["name"] if isinstance(f, dict) else f.name for f in ferramentas]


def _por_nome(ferramentas, nome):
    return next(f for f in ferramentas if (f["name"] if isinstance(f, dict) else f.name) == nome)


# --- montar_ferramentas --------------------------------------------------------------------
def test_ferramentas_por_papel(tmp_path):
    sintese = montar_ferramentas(_contexto(tmp_path, "sintese"))
    assert _nomes(sintese) == ["gravar_arquivo", "ler_artefato", "concluir_etapa"]
    assert not any(isinstance(f, dict) for f in sintese)  # sem ferramentas de servidor

    contexto = montar_ferramentas(_contexto(tmp_path, "contexto"))
    assert {"web_search", "web_fetch", "gravar_arquivo", "ler_artefato", "concluir_etapa"} == set(
        _nomes(contexto)
    )
    assert "executar_python" not in _nomes(contexto)
    assert _por_nome(contexto, "web_search") == {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 20,
    }
    assert _por_nome(contexto, "web_fetch") == {
        "type": "web_fetch_20260209",
        "name": "web_fetch",
        "max_uses": 30,
    }

    coleta = montar_ferramentas(_contexto(tmp_path, "coleta_dados"))
    assert {
        "web_fetch",
        "executar_python",
        "gravar_arquivo",
        "ler_artefato",
        "concluir_etapa",
    } == set(_nomes(coleta))
    assert "web_search" not in _nomes(coleta)


def test_documentos_e_slides_mapeiam_para_executar_python_sem_duplicar(tmp_path):
    relatorio = montar_ferramentas(_contexto(tmp_path, "relatorio"))
    assert _nomes(relatorio).count("executar_python") == 1

    etapa = replace(
        POR_CODIGO["relatorio"],
        papel_agente=replace(
            POR_CODIGO["relatorio"].papel_agente, ferramentas=("python", "documentos", "slides")
        ),
    )
    ferramentas = montar_ferramentas(_contexto(tmp_path, "relatorio", etapa=etapa))
    nomes = _nomes(ferramentas)
    assert len(nomes) == len(set(nomes)) and nomes.count("executar_python") == 1


def test_ferramenta_desconhecida_e_erro_explicito(tmp_path):
    base = POR_CODIGO["sintese"]
    etapa = replace(base, papel_agente=replace(base.papel_agente, ferramentas=("telepatia",)))
    with pytest.raises(ValueError, match="telepatia"):
        montar_ferramentas(_contexto(tmp_path, "sintese", etapa=etapa))


def test_max_uses_nao_altera_as_constantes_do_llm(tmp_path):
    from ais4.llm.cliente import FERRAMENTA_BUSCA_WEB

    montar_ferramentas(_contexto(tmp_path, "contexto"))
    assert "max_uses" not in FERRAMENTA_BUSCA_WEB


def test_ferramentas_locais_sao_estritas_sem_propriedades_livres(tmp_path):
    for ferramenta in montar_ferramentas(_contexto(tmp_path, "coleta_dados")):
        if isinstance(ferramenta, dict):
            continue
        definicao = ferramenta.to_dict()
        assert definicao["strict"] is True
        schema = definicao["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert all(p["type"] == "string" for p in schema["properties"].values())


# --- ancestrais ----------------------------------------------------------------------------
def test_ancestrais_e_o_fecho_transitivo():
    assert ancestrais(ETAPAS, "contexto") == set()
    assert ancestrais(ETAPAS, "coleta_dados") == {"contexto"}
    assert ancestrais(ETAPAS, "analise_exploratoria") == {
        "contexto",
        "coleta_dados",
        "harmonizacao",
    }
    sintese = ancestrais(ETAPAS, "sintese")
    assert "sintese" not in sintese and "modelagem" in sintese and "contexto" in sintese


# --- executar_python -----------------------------------------------------------------------
def test_executar_python_roda_no_sandbox_e_guarda_o_script(tmp_path):
    contexto = _contexto(tmp_path, "harmonizacao")
    executar = _por_nome(montar_ferramentas(contexto), "executar_python")

    texto = executar(codigo="print('ola')")

    assert contexto.sandbox.chamadas == [{"script": "print('ola')", "rede": False, "semente": 42}]
    assert "codigo_saida: 0" in texto and "ola" in texto and "saida.csv" in texto
    (ref,) = contexto.scripts
    assert ref.tipo == "codigo" and ref.nome == "_script_1.py"
    with contexto.armazem.abrir(ref.uri) as f:
        assert f.read() == b"print('ola')"
    assert contexto.armazem.verificar(ref.uri, ref.sha256)
    assert contexto.execucoes[0].ambiente["semente"] == 42


def test_executar_python_rede_so_na_coleta_de_dados(tmp_path):
    coleta = _contexto(tmp_path, "coleta_dados")
    _por_nome(montar_ferramentas(coleta), "executar_python")(codigo="x")
    assert coleta.sandbox.chamadas[0]["rede"] is True

    outra = _contexto(tmp_path, "modelagem")
    _por_nome(montar_ferramentas(outra), "executar_python")(codigo="x")
    assert outra.sandbox.chamadas[0]["rede"] is False


def test_executar_python_mostra_so_o_fim_da_saida_e_o_erro(tmp_path):
    sandbox = SandboxFalso(stdout="x" * 20000 + "FIM_OUT", stderr="Traceback...\nBoom", codigo=1)
    contexto = _contexto(tmp_path, sandbox=sandbox)
    texto = _por_nome(montar_ferramentas(contexto), "executar_python")(codigo="x")

    assert "codigo_saida: 1" in texto and "FIM_OUT" in texto and "Boom" in texto
    assert len(texto) < 15000


# --- ler_artefato / materializar_entradas --------------------------------------------------
def _ref(contexto, nome, dados: bytes, tipo="outro") -> ArtefatoRef:
    uri, sha = contexto.armazem.salvar(dados)
    return ArtefatoRef(uri=uri, sha256=sha, nome=nome, tipo=tipo)


def test_ler_artefato_devolve_texto(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["nota"] = _ref(contexto, "nota", "olá, mundo".encode())
    ler = _por_nome(montar_ferramentas(contexto), "ler_artefato")
    assert ler(nome="nota") == "olá, mundo"


def test_ler_artefato_trunca_em_50000_com_nota(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["grande"] = _ref(contexto, "grande", ("é" * 60000).encode())
    texto = _por_nome(montar_ferramentas(contexto), "ler_artefato")(nome="grande")
    corpo, _, nota = texto.partition("\n[")
    assert corpo == "é" * 50000 and "truncado" in nota


def test_ler_artefato_desconhecido_lista_nomes(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["nota"] = _ref(contexto, "nota", b"x")
    texto = _por_nome(montar_ferramentas(contexto), "ler_artefato")(nome="fantasma")
    assert "fantasma" in texto and "nota" in texto


def test_ler_artefato_sem_nenhum_disponivel(tmp_path):
    texto = _por_nome(montar_ferramentas(_contexto(tmp_path)), "ler_artefato")(nome="x")
    assert "nenhum" in texto.lower()


def test_ler_artefato_binario_explica_em_vez_de_levantar(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["img"] = _ref(contexto, "img", bytes([0x89, 0x50, 0xFF, 0x00]))
    texto = _por_nome(montar_ferramentas(contexto), "ler_artefato")(nome="img")
    assert "binário" in texto and "entradas/img" in texto


def test_materializar_entradas_copia_para_workspace(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["dados_brutos"] = _ref(contexto, "dados_brutos", b"a,b\n1,2\n")
    contexto.artefatos_disponiveis["nota"] = _ref(contexto, "nota", b"texto")

    caminhos = materializar_entradas(contexto)

    destino = contexto.workspace / "entradas"
    assert sorted(caminhos) == [destino / "dados_brutos", destino / "nota"]
    assert (destino / "dados_brutos").read_bytes() == b"a,b\n1,2\n"


def test_materializar_entradas_rejeita_nome_com_separador(tmp_path):
    contexto = _contexto(tmp_path)
    contexto.artefatos_disponiveis["../fuga"] = _ref(contexto, "x", b"x")
    with pytest.raises(ValueError):
        materializar_entradas(contexto)
    assert not (tmp_path / "fuga").exists()


# --- concluir_etapa ------------------------------------------------------------------------
def _concluir(contexto):
    return _por_nome(montar_ferramentas(contexto), "concluir_etapa")


def test_concluir_etapa_salva_no_armazem_e_grava_saida(tmp_path):
    contexto = _contexto(tmp_path, "harmonizacao")
    (contexto.workspace / "dados").mkdir()
    (contexto.workspace / "dados" / "final.csv").write_bytes(b"a\n1\n")
    (contexto.workspace / "dic.json").write_text("{}")
    (contexto.workspace / "log.txt").write_text("ok")

    msg = _concluir(contexto)(
        artefatos_json=json.dumps(
            {
                "dataset_analitico": "dados/final.csv",
                "dicionario_dados": "/work/dic.json",
                "log_tratamento": "log.txt",
            }
        ),
        resumo="feito",
    )

    saida = contexto.saida
    assert saida is not None and saida.resumo == "feito"
    assert set(saida.artefatos) == {"dataset_analitico", "dicionario_dados", "log_tratamento"}
    ref = saida.artefatos["dataset_analitico"]
    assert ref.nome == "dataset_analitico" and ref.tipo == "dataset"
    with contexto.armazem.abrir(ref.uri) as f:
        assert f.read() == b"a\n1\n"
    assert saida.artefatos["dicionario_dados"].tipo == "dataset"  # .json
    assert saida.artefatos["log_tratamento"].tipo == "outro"
    assert "ausente" not in msg.lower() and "concluída" in msg.lower()


def test_concluir_etapa_relata_saidas_esperadas_ausentes(tmp_path):
    contexto = _contexto(tmp_path, "harmonizacao")
    (contexto.workspace / "d.csv").write_text("a")

    msg = _concluir(contexto)(artefatos_json='{"dataset_analitico": "d.csv"}', resumo="parcial")

    assert contexto.saida is not None
    assert "dicionario_dados" in msg and "log_tratamento" in msg


def test_concluir_etapa_arquivo_inexistente_retorna_erro(tmp_path):
    contexto = _contexto(tmp_path)

    msg = _concluir(contexto)(artefatos_json='{"dataset_analitico": "nao_existe.csv"}', resumo="x")

    assert msg.startswith("Erro") and "nao_existe.csv" in msg
    assert contexto.saida is None


@pytest.mark.parametrize(
    "caminho", ["../fora.txt", "/etc/passwd", "a/../../fora.txt", "C:/Windows/win.ini"]
)
def test_concluir_etapa_rejeita_caminho_fora_do_workspace(tmp_path, caminho):
    (tmp_path / "fora.txt").write_text("segredo")
    contexto = _contexto(tmp_path)

    msg = _concluir(contexto)(artefatos_json=json.dumps({"x": caminho}), resumo="x")

    assert msg.startswith("Erro") and contexto.saida is None


@pytest.mark.parametrize(
    "entrada",
    [
        "isto nao e json",
        "[1, 2]",
        '{"a": 1}',
        '{"a/b": "x.txt"}',
        '{"": "x.txt"}',
        '{"..": "x.txt"}',
    ],
)
def test_concluir_etapa_entrada_invalida_retorna_erro(tmp_path, entrada):
    contexto = _contexto(tmp_path)
    (contexto.workspace / "x.txt").write_text("x")
    msg = _concluir(contexto)(artefatos_json=entrada, resumo="x")
    assert msg.startswith("Erro") and contexto.saida is None


def test_concluir_etapa_diretorio_pede_empacotamento(tmp_path):
    contexto = _contexto(tmp_path)
    (contexto.workspace / "pasta").mkdir()
    msg = _concluir(contexto)(artefatos_json='{"dados_brutos": "pasta"}', resumo="x")
    assert msg.startswith("Erro") and "zip" in msg.lower()


def test_concluir_etapa_tudo_ou_nada(tmp_path):
    contexto = _contexto(tmp_path)
    (contexto.workspace / "bom.txt").write_text("ok")

    msg = _concluir(contexto)(artefatos_json='{"a": "bom.txt", "b": "ruim.txt"}', resumo="x")

    assert msg.startswith("Erro") and contexto.saida is None
    assert not any(p.is_file() for p in contexto.armazem.raiz.rglob("*"))


def test_concluir_etapa_de_novo_substitui_a_saida(tmp_path):
    contexto = _contexto(tmp_path)
    (contexto.workspace / "v1.txt").write_text("1")
    (contexto.workspace / "v2.txt").write_text("2")
    concluir = _concluir(contexto)

    concluir(artefatos_json='{"nota_de_contexto": "v1.txt"}', resumo="primeira")
    concluir(artefatos_json='{"nota_de_contexto": "v2.txt"}', resumo="segunda")

    assert contexto.saida.resumo == "segunda"
    with contexto.armazem.abrir(contexto.saida.artefatos["nota_de_contexto"].uri) as f:
        assert f.read() == b"2"


@pytest.mark.parametrize(
    ("nome", "arquivo", "tipo"),
    [
        ("script", "a.py", "codigo"),
        ("dados", "d.csv", "dataset"),
        ("dados", "d.parquet", "dataset"),
        ("dados", "d.xlsx", "dataset"),
        ("cenarios", "c.json", "dataset"),
        ("grafico", "g.PNG", "grafico"),
        ("grafico", "g.svg", "grafico"),
        ("grafico", "g.jpg", "grafico"),
        ("apresentacao", "s.pptx", "apresentacao"),
        ("relatorio", "r.docx", "relatorio"),
        ("nota", "relatorio_final.pdf", "relatorio"),
        ("relatorio", "r.md", "relatorio"),
        ("nota_de_contexto", "n.md", "outro"),
        ("achados", "a.pdf", "outro"),
        ("dados_brutos", "d.zip", "dataset"),
        ("log", "l.txt", "outro"),
    ],
)
def test_tipo_do_artefato_por_extensao_e_nome(nome, arquivo, tipo):
    assert tipo_do_artefato(nome, Path(arquivo)) == tipo


# --- gravar_arquivo ------------------------------------------------------------------------
def test_gravar_arquivo_cria_pastas_e_sobrescreve(tmp_path):
    contexto = _contexto(tmp_path, "sintese")
    gravar = _por_nome(montar_ferramentas(contexto), "gravar_arquivo")

    assert "gravado" in gravar(caminho="saidas/achados.md", conteudo="v1 açaí")
    assert "gravado" in gravar(caminho="/work/saidas/achados.md", conteudo="v2")

    assert (contexto.workspace / "saidas" / "achados.md").read_text(encoding="utf-8") == "v2"


def test_gravar_arquivo_diretorio_existente_e_erro(tmp_path):
    contexto = _contexto(tmp_path, "sintese")
    (contexto.workspace / "pasta").mkdir()
    msg = _por_nome(montar_ferramentas(contexto), "gravar_arquivo")(caminho="pasta", conteudo="x")
    assert msg.startswith("Erro")
