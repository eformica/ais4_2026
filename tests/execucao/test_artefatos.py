import hashlib
from pathlib import Path

import pytest

from ais4.execucao.artefatos import Armazem


@pytest.fixture
def armazem(tmp_path) -> Armazem:
    return Armazem(tmp_path / "armazem")


def test_armazem_enderecado_por_conteudo(armazem):
    uri1, sha1 = armazem.salvar(b"conteudo igual")
    uri2, sha2 = armazem.salvar(b"conteudo igual")
    uri3, _ = armazem.salvar(b"outro conteudo")

    assert (uri1, sha1) == (uri2, sha2)
    assert uri3 != uri1
    assert sha1 == hashlib.sha256(b"conteudo igual").hexdigest()
    assert uri1.startswith("file://")
    esperado = armazem.raiz / sha1[:2] / sha1
    assert uri1 == esperado.as_uri() and esperado.read_bytes() == b"conteudo igual"


def test_salvar_arquivo_em_blocos_sem_carregar_tudo(armazem, tmp_path, monkeypatch):
    monkeypatch.setattr("ais4.execucao.artefatos.TAMANHO_BLOCO", 1024)
    origem = tmp_path / "grande.bin"
    dados = bytes(range(256)) * 100  # 25.600 bytes = 25 blocos
    origem.write_bytes(dados)

    uri, sha = armazem.salvar(origem)

    assert sha == hashlib.sha256(dados).hexdigest()
    with armazem.abrir(uri) as f:
        assert f.read() == dados
    # nenhum temporário sobra no armazém
    assert [p.name for p in armazem.raiz.rglob("*") if p.is_file()] == [sha]


def test_verificar_hash_adulterado_false(armazem):
    uri, sha = armazem.salvar(b"original")
    assert armazem.verificar(uri, sha) is True

    Path(armazem.raiz / sha[:2] / sha).write_bytes(b"adulterado")

    assert armazem.verificar(uri, sha) is False


def test_verificar_arquivo_ausente_false(armazem):
    uri, sha = armazem.salvar(b"x")
    (armazem.raiz / sha[:2] / sha).unlink()
    assert armazem.verificar(uri, sha) is False


@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/passwd",
        "https://exemplo.com/x",
        "s3://bucket/chave",
        "caminho/relativo",
    ],
)
def test_abrir_rejeita_uri_estranha(armazem, uri):
    with pytest.raises(ValueError):
        armazem.abrir(uri)


def test_abrir_rejeita_path_traversal(armazem, tmp_path):
    armazem.salvar(b"x")
    fora = tmp_path / "fora.txt"
    fora.write_text("segredo")
    uri = (armazem.raiz / ".." / "fora.txt").as_uri()

    with pytest.raises(ValueError):
        armazem.abrir(uri)
    with pytest.raises(ValueError):
        armazem.verificar(uri, "0" * 64)


def test_raiz_padrao_vem_da_config(tmp_path, monkeypatch):
    from ais4 import config

    monkeypatch.setenv("AIS4_ARTEFATOS_DIR", str(tmp_path / "padrao"))
    config.obter_config.cache_clear()
    try:
        uri, _ = Armazem().salvar(b"a")
    finally:
        config.obter_config.cache_clear()
    assert uri.startswith((tmp_path / "padrao").resolve().as_uri())
