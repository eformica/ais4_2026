import dataclasses
from uuid import UUID, uuid4

import psycopg
import pytest

from ais4.auth import Usuario
from ais4.comum.dto import IdeiaDTO
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.comum.espec import EspecProjeto
from ais4.playbooks import registro, servico
from ais4.playbooks.base import (
    Avaliacao,
    Criterio,
    Entregavel,
    Etapa,
    PapelAgente,
    Playbook,
    Requisito,
    Resultado,
    SaidaEtapa,
)

SEMENTE_ID = "00000000-0000-4000-8000-000000000001"
SEMENTE_ID_UUID = UUID(SEMENTE_ID)


def _etapa(codigo, ordem, depende_de=(), **kw):
    campos = {
        "nome": f"Etapa {codigo}",
        "descricao": None,
        "papel_agente": PapelAgente("analista", "Você analisa dados.", ("python",)),
        "entradas": (),
        "saidas": (f"saida_{codigo}",),
        "criterio_saida": f"{codigo} concluída",
        "exige_gate": False,
        "papel_revisor": None,
    }
    campos.update(kw)
    return Etapa(codigo=codigo, ordem=ordem, depende_de=tuple(depende_de), **campos)


class PlaybookDeTeste(Playbook):
    id = "playbook-de-teste"
    versao = "0.1.0"
    nome = "Playbook de teste"
    descricao = "Só para testes"
    changelog = "Primeira versão"
    classe_de_problema = ["teste"]
    pre_requisitos = [Requisito("dados", "Alguma fonte")]
    template_projeto = {"secoes": ["escopo"]}
    etapas = [
        _etapa("a", 1),
        _etapa("b", 2, ["a"], exige_gate=True, papel_revisor="revisor_metodologico"),
    ]
    criterios_qualidade = [Criterio("rastreabilidade", "Tudo rastreável")]
    entregaveis = [Entregavel("relatorio")]

    def aplicavel(self, ideia: IdeiaDTO) -> Avaliacao:
        return Avaliacao(True, 1.0, [])

    def instanciar(self, ideia: IdeiaDTO) -> EspecProjeto:
        return EspecProjeto()

    def validar_etapa(self, etapa: Etapa, saida: SaidaEtapa) -> Resultado:
        return Resultado(True, [])


def _variante(**atributos) -> Playbook:
    """Instância de PlaybookDeTeste com atributos de classe trocados."""
    return type("Variante", (PlaybookDeTeste,), atributos)()


def _guardiao(nome):
    return Usuario(nome, frozenset({"guardiao_de_playbooks"}))


def _consultar(banco, sql, *params):
    with psycopg.connect(banco) as c:
        return c.execute(sql, params).fetchall()


def _id_do_teste():
    (versao,) = [v for v in servico.listar_versoes() if v.codigo == PlaybookDeTeste.id]
    return versao.playbook_id


def test_sincronizar_insere_versao_proposta(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")

    (linha,) = _consultar(
        banco,
        "select v.status, v.proposto_por, p.nome, p.classe_de_problema, v.criterios_qualidade, "
        "v.entregaveis, v.pre_requisitos, v.template_projeto, v.changelog "
        "from playbooks.playbook p join playbooks.playbook_versao v on v.playbook_id = p.id "
        "where p.codigo = 'playbook-de-teste'",
    )
    assert linha[:4] == ("proposto", "ana", "Playbook de teste", ["teste"])
    assert linha[4] == [{"codigo": "rastreabilidade", "descricao": "Tudo rastreável"}]
    assert linha[5] == [{"tipo": "relatorio"}]
    assert linha[6] == [{"tipo": "dados", "descricao": "Alguma fonte"}]
    assert linha[7] == {"secoes": ["escopo"]}
    assert linha[8] == "Primeira versão"

    etapas = _consultar(
        banco,
        "select e.codigo, e.ordem, e.depende_de, e.papel_agente, e.saidas, e.exige_gate, "
        "e.papel_revisor from playbooks.etapa e join playbooks.playbook p on p.id = e.playbook_id "
        "where p.codigo = 'playbook-de-teste' order by e.ordem",
    )
    assert [e[0] for e in etapas] == ["a", "b"]
    assert etapas[1][2] == ["a"]
    # R5: o prompt_base não vai para o banco
    assert etapas[0][3] == {"papel": "analista", "ferramentas": ["python"]}
    assert etapas[0][4] == ["saida_a"]
    assert etapas[1][5:] == (True, "revisor_metodologico")


def test_sincronizar_idempotente(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    servico.sincronizar(PlaybookDeTeste(), "ana")
    servico.sincronizar(PlaybookDeTeste(), "outra")

    assert _consultar(
        banco, "select count(*) from playbooks.playbook where codigo = 'playbook-de-teste'"
    ) == [(1,)]
    assert _consultar(
        banco,
        "select count(*) from playbooks.etapa e join playbooks.playbook p on p.id = e.playbook_id "
        "where p.codigo = 'playbook-de-teste'",
    ) == [(2,)]
    assert _consultar(
        banco,
        "select proposto_por from playbooks.playbook_versao v "
        "join playbooks.playbook p on p.id = v.playbook_id where p.codigo = 'playbook-de-teste'",
    ) == [("ana",)]


def test_sincronizar_nova_versao_de_playbook_existente(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    servico.sincronizar(_variante(versao="0.2.0"), "ana")

    assert _consultar(
        banco,
        "select v.versao from playbooks.playbook_versao v "
        "join playbooks.playbook p on p.id = v.playbook_id "
        "where p.codigo = 'playbook-de-teste' order by 1",
    ) == [("0.1.0",), ("0.2.0",)]
    assert _consultar(banco, "select count(*) from playbooks.playbook") == [(2,)]  # semente + teste


def test_sincronizar_versao_publicada_divergente_levanta(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    servico.alterar_status(_id_do_teste(), "0.1.0", "experimental", _guardiao("bruno"))

    alterada = [dataclasses.replace(PlaybookDeTeste.etapas[0], criterio_saida="outro critério")]
    divergente = _variante(etapas=[*alterada, PlaybookDeTeste.etapas[1]])

    with pytest.raises(RegraViolada, match="versão publicada difere da classe"):
        servico.sincronizar(divergente, "ana")

    # a classe original continua sincronizando sem erro
    servico.sincronizar(PlaybookDeTeste(), "ana")


@pytest.mark.parametrize(
    "atributos",
    [
        {"nome": "Outro nome"},
        {"descricao": "Outra descrição"},
        {"classe_de_problema": ["teste", "extra"]},
        {"pre_requisitos": []},
        {"template_projeto": {"secoes": []}},
        {"criterios_qualidade": []},
        {"entregaveis": [Entregavel("dataset")]},
        {"changelog": "Mudou"},
        {"etapas": PlaybookDeTeste.etapas[:1]},
        {"etapas": [*PlaybookDeTeste.etapas, _etapa("c", 3, ["b"])]},
        {
            "etapas": [
                PlaybookDeTeste.etapas[0],
                dataclasses.replace(PlaybookDeTeste.etapas[1], depende_de=()),
            ]
        },
        {
            "etapas": [
                PlaybookDeTeste.etapas[0],
                dataclasses.replace(
                    PlaybookDeTeste.etapas[1], papel_agente=PapelAgente("outro", "x", ("python",))
                ),
            ]
        },
        {
            "etapas": [
                PlaybookDeTeste.etapas[0],
                dataclasses.replace(PlaybookDeTeste.etapas[1], saidas=("x",)),
            ]
        },
    ],
    ids=lambda a: next(iter(a)) + str(len(str(a))),
)
def test_sincronizar_qualquer_diferenca_levanta(banco, limpar, atributos):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    with pytest.raises(RegraViolada, match="versão publicada difere da classe"):
        servico.sincronizar(_variante(**atributos), "ana")


def test_sincronizar_ignora_prompt_base_e_ordem_das_chaves(banco, limpar):
    # R5: o prompt vive só na classe; mudar o prompt não diverge do banco.
    servico.sincronizar(PlaybookDeTeste(), "ana")
    outro_prompt = dataclasses.replace(
        PlaybookDeTeste.etapas[0], papel_agente=PapelAgente("analista", "NOVO PROMPT", ("python",))
    )
    servico.sincronizar(_variante(etapas=[outro_prompt, PlaybookDeTeste.etapas[1]]), "ana")


def test_descricao_vazia_equivale_a_nulo(banco, limpar):
    servico.sincronizar(_variante(descricao=""), "ana")
    servico.sincronizar(_variante(descricao=None), "ana")


def test_quem_propos_nao_promove(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    pid = _id_do_teste()

    with pytest.raises(Proibido):
        servico.alterar_status(pid, "0.1.0", "experimental", _guardiao("ana"))
    assert servico.versoes_ativas() != [] and all(
        v.codigo != "playbook-de-teste" for v in servico.versoes_ativas()
    )

    servico.alterar_status(pid, "0.1.0", "experimental", _guardiao("bruno"))
    assert ("playbook-de-teste", "0.1.0", "experimental") in {
        (v.codigo, v.versao, v.status) for v in servico.versoes_ativas()
    }


def test_promover_exige_papel_guardiao(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    sem_papel = Usuario("bruno", frozenset({"curador_do_acervo"}))
    with pytest.raises(Proibido):
        servico.alterar_status(_id_do_teste(), "0.1.0", "experimental", sem_papel)


def test_alterar_status_versao_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        servico.alterar_status(uuid4(), "9.9.9", "experimental", _guardiao("bruno"))


def test_transicao_invalida_vira_regra_violada(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    with pytest.raises(RegraViolada, match="Transição de status de playbook inválida"):
        servico.alterar_status(_id_do_teste(), "0.1.0", "validado", _guardiao("bruno"))


def test_promover_com_dependencia_inexistente_levanta(banco, limpar):
    servico.sincronizar(_variante(etapas=[_etapa("a", 1, ["fantasma"])]), "ana")
    with pytest.raises(RegraViolada, match="fantasma"):
        servico.alterar_status(_id_do_teste(), "0.1.0", "experimental", _guardiao("bruno"))
    assert _consultar(
        banco,
        "select status from playbooks.playbook_versao where versao = '0.1.0' and playbook_id <> %s",
        SEMENTE_ID,
    ) == [("proposto",)]


def test_promover_com_ciclo_levanta(banco, limpar):
    ciclico = _variante(etapas=[_etapa("a", 1, ["b"]), _etapa("b", 2, ["a"])])
    servico.sincronizar(ciclico, "ana")
    with pytest.raises(RegraViolada, match="Ciclo"):
        servico.alterar_status(_id_do_teste(), "0.1.0", "experimental", _guardiao("bruno"))


def test_promocao_gera_evento_no_outbox_com_autor(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    servico.alterar_status(_id_do_teste(), "0.1.0", "experimental", _guardiao("bruno"))
    (linha,) = _consultar(
        banco,
        "select payload->>'para', payload->>'autor' from playbooks.outbox "
        "where tipo_evento = 'PlaybookVersaoStatusAlterado' and agregado_id = %s",
        _id_do_teste(),
    )
    assert linha == ("experimental", "bruno")


def test_versoes_ativas_inclui_a_semente(banco, limpar):
    ativas = servico.versoes_ativas()
    assert [(v.codigo, v.versao, v.status) for v in ativas] == [
        ("projeto-academico-simples", "0.1.0", "experimental")
    ]
    assert str(ativas[0].playbook_id) == SEMENTE_ID


def test_etapas_da_semente_ordenadas(banco, limpar):
    with registro.isolado(vazio=True):
        etapas = servico.etapas(SEMENTE_ID_UUID, "0.1.0")
    assert [e.ordem for e in etapas] == list(range(1, 9))
    assert etapas[0].codigo == "contexto"
    assert etapas[1].depende_de == ("contexto",)
    assert etapas[1].exige_gate and etapas[1].papel_revisor == "responsavel_por_dados"
    assert etapas[1].papel_agente.ferramentas == ("web_fetch", "python")
    assert etapas[1].papel_agente.prompt_base == ""  # sem classe registrada


def test_etapas_da_semente_trazem_o_prompt_da_classe_registrada(banco, limpar):
    etapas = servico.etapas(SEMENTE_ID_UUID, "0.1.0")  # o import do pacote registra a classe
    assert all("concluir_etapa" in e.papel_agente.prompt_base for e in etapas)


def test_etapas_completa_prompt_base_a_partir_do_registro(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    with registro.isolado():
        registro.registrar(PlaybookDeTeste)
        etapas = servico.etapas(_id_do_teste(), "0.1.0")
    assert etapas[0].papel_agente.prompt_base == "Você analisa dados."
    assert etapas == PlaybookDeTeste.etapas


def test_etapas_de_versao_inexistente(banco, limpar):
    with pytest.raises(NaoEncontrado):
        servico.etapas(SEMENTE_ID_UUID, "9.9.9")


def test_registrar_proposta_melhoria_idempotente(banco, limpar):
    licao, projeto = uuid4(), uuid4()
    primeiro = servico.registrar_proposta_melhoria(
        SEMENTE_ID_UUID, "0.1.0", "Adicionar etapa de validação", licao, projeto, "sistema"
    )
    segundo = servico.registrar_proposta_melhoria(
        SEMENTE_ID_UUID, "0.1.0", "Texto diferente", licao, projeto, "sistema"
    )
    assert primeiro == segundo
    assert _consultar(
        banco,
        "select count(*), min(status), min(descricao) from playbooks.proposta_melhoria",
    ) == [(1, "pendente", "Adicionar etapa de validação")]
    outra = servico.registrar_proposta_melhoria(
        SEMENTE_ID_UUID, "0.1.0", "Outra lição", uuid4(), projeto, "sistema"
    )
    assert outra != primeiro


def test_registrar_proposta_melhoria_versao_base_inexistente(banco, limpar):
    with pytest.raises(RegraViolada):
        servico.registrar_proposta_melhoria(
            SEMENTE_ID_UUID, "9.9.9", "x", uuid4(), uuid4(), "sistema"
        )


def test_proposto_por_nao_pode_ser_trocado(banco, limpar):
    servico.sincronizar(PlaybookDeTeste(), "ana")
    with psycopg.connect(banco) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("update playbooks.playbook_versao set proposto_por = 'bruno'")
