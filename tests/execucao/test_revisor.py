import json

from ais4.comum.dto import ArtefatoRef
from ais4.execucao.artefatos import Armazem
from ais4.execucao.revisor import LIMITE_TEXTO, PAPEL_REVISOR, revisar
from ais4.llm.cliente import RespostaInvalida
from ais4.llm.falso import ClienteLLMFalso
from ais4.playbooks.academico_simples import ProjetoAcademicoSimples
from ais4.playbooks.base import SaidaEtapa

PLAYBOOK = ProjetoAcademicoSimples()
CRITERIOS = PLAYBOOK.criterios_qualidade
CONTEXTO = next(e for e in PLAYBOOK.etapas if e.codigo == "contexto")


def _ref(armazem: Armazem, nome: str, dados: bytes) -> ArtefatoRef:
    uri, sha = armazem.salvar(dados)
    return ArtefatoRef(uri=uri, sha256=sha, nome=nome, tipo="outro")


def _parecer(aprovado=True, feedback=(), falhos=()) -> str:
    return json.dumps(
        {
            "aprovado": aprovado,
            "feedback": list(feedback),
            "por_criterio": {c.codigo: c.codigo not in falhos for c in CRITERIOS},
        }
    )


def _usos():
    usos = []
    return usos, usos.append


def _revisar(tmp_path, cliente, artefatos: dict[str, bytes] | None = None, ao_usar=None):
    armazem = Armazem(tmp_path / "armazem")
    conteudo = artefatos if artefatos is not None else {"nota_de_contexto": b"# Nota\nTexto."}
    saida = SaidaEtapa(
        artefatos={n: _ref(armazem, n, d) for n, d in conteudo.items()}, resumo="resumo do agente"
    )
    return revisar(
        PLAYBOOK, CONTEXTO, saida, CRITERIOS, cliente, ao_usar or (lambda u: None), armazem=armazem
    )


def test_validacao_deterministica_reprova_sem_chamar_o_llm(tmp_path):
    cliente = ClienteLLMFalso({})
    resultado = _revisar(tmp_path, cliente, artefatos={})
    assert not resultado.aprovado
    assert any("nota_de_contexto" in f for f in resultado.feedback)
    assert cliente.chamadas == []


def test_revisor_aprova_com_resumo_texto_e_criterios(tmp_path):
    usos, ao_usar = _usos()
    cliente = ClienteLLMFalso({PAPEL_REVISOR: [_parecer()]})

    resultado = _revisar(tmp_path, cliente, ao_usar=ao_usar)

    assert resultado.aprovado and resultado.feedback == []
    (chamada,) = cliente.chamadas
    assert (chamada.metodo, chamada.papel) == ("estruturado", "revisor")
    for trecho in (
        "resumo do agente",
        "# Nota\nTexto.",
        CONTEXTO.criterio_saida,
        "rastreabilidade",
        CRITERIOS[0].descricao,
    ):
        assert trecho in chamada.mensagem
    assert set(chamada.formato.model_fields["por_criterio"].annotation.model_fields) == {
        c.codigo for c in CRITERIOS
    }
    assert len(usos) == 1


def test_revisor_reprova_com_feedback_e_criterios_falhos(tmp_path):
    cliente = ClienteLLMFalso(
        {PAPEL_REVISOR: [_parecer(False, ["Faltam as URLs"], falhos=("rastreabilidade",))]}
    )
    resultado = _revisar(tmp_path, cliente)
    assert not resultado.aprovado
    assert resultado.feedback[0] == "Faltam as URLs"
    assert any("rastreabilidade" in f for f in resultado.feedback[1:])


def test_criterio_nao_atendido_reprova_mesmo_com_aprovado_verdadeiro(tmp_path):
    cliente = ClienteLLMFalso({PAPEL_REVISOR: [_parecer(True, falhos=("limitacoes",))]})
    resultado = _revisar(tmp_path, cliente)
    assert not resultado.aprovado and any("limitacoes" in f for f in resultado.feedback)


def test_reprovacao_sem_feedback_ganha_motivo(tmp_path):
    cliente = ClienteLLMFalso({PAPEL_REVISOR: [_parecer(False)]})
    resultado = _revisar(tmp_path, cliente)
    assert not resultado.aprovado and resultado.feedback


def test_resposta_invalida_do_revisor_conta_como_reprovacao(tmp_path):
    cliente = ClienteLLMFalso({PAPEL_REVISOR: [RespostaInvalida("refusal", "não posso")]})
    resultado = _revisar(tmp_path, cliente)
    assert not resultado.aprovado
    assert resultado.feedback == ["revisão indisponível: refusal: não posso"]


def test_artefatos_de_texto_ate_50000_caracteres_e_binarios_omitidos(tmp_path):
    cliente = ClienteLLMFalso({PAPEL_REVISOR: [_parecer()]})
    _revisar(
        tmp_path,
        cliente,
        artefatos={
            "nota_de_contexto": ("§" * 40000).encode(),
            "figura": bytes([0x89, 0x50, 0x00, 0xFF]),
            "anexo": ("¶" * 30000).encode(),
            "apendice": b"depois do limite",
        },
    )
    mensagem = cliente.chamadas[0].mensagem
    assert mensagem.count("§") == 40000
    assert mensagem.count("¶") < 30000  # o orçamento de texto acabou no segundo artefato
    assert mensagem.count("§") + mensagem.count("¶") <= LIMITE_TEXTO
    assert "truncado" in mensagem
    assert "figura" in mensagem and "binário" in mensagem
    assert "depois do limite" not in mensagem and "apendice" in mensagem
