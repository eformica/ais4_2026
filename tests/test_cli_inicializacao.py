"""Revisão final #6: os processos que executam playbooks sincronizam as classes na subida."""

import pytest

from ais4 import __main__ as cli
from ais4.playbooks import registro
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from tests.projetos.fabrica import consultar

COMANDOS = {
    "api": ("uvicorn", "run"),
    "worker-outbox": ("eventos", "loop"),
    "motor-criacao": ("motor", "loop"),
    "worker-execucao": ("orquestrador", "loop"),
}


@pytest.fixture
def subida(monkeypatch):
    """Substitui o que cada comando sobe; devolve a lista dos que chegaram a subir."""
    subiram: list[str] = []
    monkeypatch.setattr(cli, "ClienteAnthropic", lambda: "cliente")
    monkeypatch.setattr(cli.eventos, "carregar_handlers", lambda: None)
    for comando, (modulo, funcao) in COMANDOS.items():
        alvo = getattr(cli, modulo)
        monkeypatch.setattr(alvo, funcao, lambda *a, _c=comando, **k: subiram.append(_c))
    return subiram


@pytest.mark.parametrize("comando", list(COMANDOS))
def test_cada_processo_sincroniza_antes_de_subir(monkeypatch, subida, comando):
    ordem: list[str] = []
    monkeypatch.setattr(cli, "_sincronizar_playbooks", lambda: ordem.append("sincronizar") or 0)

    assert cli.main([comando]) == 0

    assert ordem == ["sincronizar"]
    assert subida == [comando]


@pytest.mark.parametrize("comando", list(COMANDOS))
def test_divergencia_impede_a_subida_com_codigo_3(monkeypatch, subida, comando):
    monkeypatch.setattr(cli, "_sincronizar_playbooks", lambda: 3)
    assert cli.main([comando]) == 3
    assert subida == []


def test_migrar_e_revisao_nao_sincronizam(monkeypatch):
    monkeypatch.setattr(cli, "_sincronizar_playbooks", lambda: pytest.fail("não deveria"))
    monkeypatch.setattr(cli.command, "upgrade", lambda *a, **k: None)
    assert cli.main(["migrar"]) == 0


def test_sincronizacao_real_com_a_semente_passa_e_insere_versao_nova(banco, limpar):
    class VersaoNova(ProjetoAcademicoSimples):
        versao = "0.2.0"
        changelog = "Versão de teste"

    with registro.isolado():
        registro.registrar(VersaoNova)
        assert cli._sincronizar_playbooks() == 0

    assert consultar(
        banco,
        "select status, proposto_por from playbooks.playbook_versao where versao = '0.2.0'",
    ) == [("proposto", "sistema:inicializacao")]


def test_sincronizacao_real_com_classe_editada_sai_com_3(banco, limpar, capsys, caplog):
    class Editada(ProjetoAcademicoSimples):
        changelog = "Editado sem nova versão"

    with registro.isolado(vazio=True):
        registro.registrar(Editada)
        assert cli._sincronizar_playbooks() == 3

    erro = capsys.readouterr().err
    assert "projeto-academico-simples 0.1.0" in erro and "nova versão" in erro
    assert "versao.changelog" in caplog.text  # o campo divergente fica no log
