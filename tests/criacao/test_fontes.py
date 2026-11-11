import dataclasses

import anthropic
import httpx
import pytest

from ais4.criacao import agentes, motor
from ais4.criacao.modelos import FonteVerificada
from ais4.llm.cliente import OrcamentoExcedido, RespostaInvalida
from ais4.llm.falso import ClienteLLMFalso
from tests.criacao.fabrica import consultar, qualificar_semente
from tests.playbooks.semente import IDEIA_SEMENTE

PAPEL = "verificador_fontes"
CENSO = "IBGE - Censo 2022"
PAM = "IBGE - Produção Agrícola Municipal (PAM)"


def _preparar(*nomes: str, urls: dict[str, str] | None = None):
    """Ideia da semente com as fontes pedidas, já reservada por uma proposta."""
    qualificar_semente()
    ideia, proposta_id = motor.selecionar_e_reservar("motor-1")
    fontes = [{"nome": n, **({"url": urls[n]} if urls and n in urls else {})} for n in nomes]
    return dataclasses.replace(ideia, fontes_candidatas=fontes), proposta_id


def _registra(**campos):
    """Roteiro: o agente registra a verificação uma vez e responde."""

    def roteiro(ferramentas):
        ferramentas["registrar_verificacao"](**campos)
        return "registrado"

    return roteiro


def _acessivel(nome=CENSO, url="https://www.ibge.gov.br/censo"):
    return _registra(
        nome=nome,
        url=url,
        status="acessivel",
        cobertura={"temporal": "2022", "geografica": "municípios", "variaveis": ["população"]},
        evidencia="web_fetch abriu a página do censo.",
    )


def _linhas(banco, proposta_id):
    return consultar(
        banco,
        "select nome, url, status, cobertura, evidencia from criacao.fonte_verificada "
        "where proposta_id = %s order by nome",
        (proposta_id,),
    )


def test_fonte_acessivel_e_gravada_com_cobertura(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    cliente = ClienteLLMFalso({PAPEL: [_acessivel()]})

    resultado = motor.verificar_fontes(proposta_id, ideia, cliente)

    assert resultado == [
        FonteVerificada(
            nome=CENSO,
            url="https://www.ibge.gov.br/censo",
            status="acessivel",
            cobertura={"temporal": "2022", "geografica": "municípios", "variaveis": ["população"]},
            evidencia="web_fetch abriu a página do censo.",
        )
    ]
    (linha,) = _linhas(banco, proposta_id)
    assert linha[:3] == (CENSO, "https://www.ibge.gov.br/censo", "acessivel")
    assert linha[3]["temporal"] == "2022"


def test_agente_recebe_prompt_ferramentas_e_a_fonte(banco, limpar):
    ideia, proposta_id = _preparar(CENSO, urls={CENSO: "https://ibge.gov.br/c"})
    cliente = ClienteLLMFalso({PAPEL: [_acessivel(url="https://ibge.gov.br/c")]})

    motor.verificar_fontes(proposta_id, ideia, cliente)

    (chamada,) = cliente.chamadas
    assert chamada.metodo == "agente"
    assert chamada.sistema == agentes.PROMPT_VERIFICADOR_FONTES
    assert set(chamada.ferramentas) == {"web_search", "web_fetch", "registrar_verificacao"}
    assert CENSO in chamada.mensagem and "https://ibge.gov.br/c" in chamada.mensagem
    assert ideia.pergunta_norteadora in chamada.mensagem


def test_mensagem_de_fonte_sem_url_pede_para_localizar(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    cliente = ClienteLLMFalso({PAPEL: [_acessivel()]})

    motor.verificar_fontes(proposta_id, ideia, cliente)

    assert "sem URL" in cliente.chamadas[0].mensagem


def test_fonte_sem_url_nao_localizada_nao_quebra(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    nao_achou = _registra(
        nome=CENSO,
        url=None,
        status="nao_localizada",
        cobertura="não confirmada",
        evidencia="web_search não retornou página oficial.",
    )
    cliente = ClienteLLMFalso({PAPEL: [nao_achou]})

    (fonte,) = motor.verificar_fontes(proposta_id, ideia, cliente)

    assert fonte.status == "nao_localizada" and fonte.url is None
    assert fonte.cobertura == {"descricao": "não confirmada"}
    (linha,) = _linhas(banco, proposta_id)
    assert linha[:3] == (CENSO, None, "nao_localizada")


def test_uma_chamada_do_agente_por_fonte_e_uso_repassado(banco, limpar):
    ideia, proposta_id = _preparar(CENSO, PAM)
    cliente = ClienteLLMFalso({PAPEL: [_acessivel(CENSO), _acessivel(PAM, "https://sidra/pam")]})
    usos = []

    resultado = motor.verificar_fontes(proposta_id, ideia, cliente, usos.append)

    assert [f.nome for f in resultado] == [CENSO, PAM]
    assert len(cliente.chamadas) == 2
    assert len(usos) == 4  # (1 ferramenta + 1 final) por fonte
    assert len(_linhas(banco, proposta_id)) == 2


def test_sem_ao_usar_o_registro_de_uso_e_noop(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    motor.verificar_fontes(proposta_id, ideia, ClienteLLMFalso({PAPEL: [_acessivel()]}))
    assert motor.registrar_uso_noop(None) is None


def test_resposta_invalida_na_verificacao_marca_fonte_inacessivel(banco, limpar):
    ideia, proposta_id = _preparar(CENSO, PAM)
    cliente = ClienteLLMFalso(
        {PAPEL: [RespostaInvalida("max_tokens", "estourou"), _acessivel(PAM, "https://sidra/pam")]}
    )

    resultado = motor.verificar_fontes(proposta_id, ideia, cliente)

    censo, pam = resultado
    assert censo.status == "inacessivel" and censo.nome == CENSO
    assert "max_tokens" in censo.evidencia
    assert pam.status == "acessivel"  # a falha de uma fonte não derruba as demais
    assert [linha[2] for linha in _linhas(banco, proposta_id)] == ["inacessivel", "acessivel"]


def test_agente_que_termina_sem_registrar_marca_inacessivel(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    cliente = ClienteLLMFalso({PAPEL: ["Não consegui concluir."]})

    (fonte,) = motor.verificar_fontes(proposta_id, ideia, cliente)

    assert fonte.status == "inacessivel"
    assert "registrar_verificacao" in fonte.evidencia


def test_erro_da_api_marca_inacessivel(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    erro = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.exemplo"))
    cliente = ClienteLLMFalso({PAPEL: [erro]})

    (fonte,) = motor.verificar_fontes(proposta_id, ideia, cliente)

    assert fonte.status == "inacessivel"
    assert "APIConnectionError" in fonte.evidencia


def test_orcamento_excedido_propaga_e_preserva_o_que_ja_foi_verificado(banco, limpar):
    ideia, proposta_id = _preparar(CENSO, PAM)
    cliente = ClienteLLMFalso({PAPEL: [_acessivel(CENSO), OrcamentoExcedido("acabou")]})

    with pytest.raises(OrcamentoExcedido):
        motor.verificar_fontes(proposta_id, ideia, cliente)

    assert [linha[0] for linha in _linhas(banco, proposta_id)] == [CENSO]


def test_ferramenta_devolve_erro_ao_modelo_quando_a_entrada_e_invalida(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    respostas = []

    def roteiro(ferramentas):
        registrar = ferramentas["registrar_verificacao"]
        respostas.append(
            registrar(nome=CENSO, url="https://x", status="talvez", cobertura={}, evidencia="e")
        )
        respostas.append(
            registrar(nome=CENSO, url=None, status="acessivel", cobertura={}, evidencia="e")
        )
        respostas.append(
            registrar(nome=CENSO, url="https://x", status="acessivel", cobertura={}, evidencia="ok")
        )
        return "registrado"

    (fonte,) = motor.verificar_fontes(proposta_id, ideia, ClienteLLMFalso({PAPEL: [roteiro]}))

    assert respostas[0].startswith("Erro") and "status" in respostas[0]
    assert respostas[1].startswith("Erro") and "URL" in respostas[1]
    assert not respostas[2].startswith("Erro")
    assert (fonte.status, fonte.evidencia) == ("acessivel", "ok")


def test_registrar_duas_vezes_vale_o_primeiro_registro(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    respostas = []

    def roteiro(ferramentas):
        registrar = ferramentas["registrar_verificacao"]
        respostas.append(
            registrar(nome=CENSO, url="https://x", status="acessivel", cobertura={}, evidencia="1")
        )
        respostas.append(
            registrar(nome=CENSO, url=None, status="nao_localizada", cobertura={}, evidencia="2")
        )
        return "registrado"

    (fonte,) = motor.verificar_fontes(proposta_id, ideia, ClienteLLMFalso({PAPEL: [roteiro]}))

    assert fonte.evidencia == "1"
    assert "já" in respostas[1]


def test_nome_gravado_e_o_da_fonte_candidata(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    cliente = ClienteLLMFalso({PAPEL: [_acessivel(nome="Censo Demográfico 2022 (IBGE)")]})

    (fonte,) = motor.verificar_fontes(proposta_id, ideia, cliente)

    assert fonte.nome == CENSO
    assert _linhas(banco, proposta_id)[0][0] == CENSO


def test_nova_verificacao_substitui_a_anterior(banco, limpar):
    ideia, proposta_id = _preparar(CENSO)
    motor.verificar_fontes(proposta_id, ideia, ClienteLLMFalso({PAPEL: ["sem registro"]}))
    motor.verificar_fontes(proposta_id, ideia, ClienteLLMFalso({PAPEL: [_acessivel()]}))

    (linha,) = _linhas(banco, proposta_id)
    assert linha[2] == "acessivel"


def test_ideia_sem_fontes_nao_chama_o_agente(banco, limpar):
    ideia, proposta_id = _preparar()
    cliente = ClienteLLMFalso({})

    assert motor.verificar_fontes(proposta_id, ideia, cliente) == []
    assert cliente.chamadas == []


def test_semente_com_dez_fontes_sem_url(banco, limpar):
    qualificar_semente()
    ideia, proposta_id = motor.selecionar_e_reservar("motor-1")
    assert len(ideia.fontes_candidatas) == len(IDEIA_SEMENTE.fontes_candidatas) == 10
    roteiro = [
        _registra(nome=f["nome"], url=None, status="nao_localizada", cobertura={}, evidencia="-")
        for f in ideia.fontes_candidatas
    ]

    resultado = motor.verificar_fontes(proposta_id, ideia, ClienteLLMFalso({PAPEL: roteiro}))

    assert {f.status for f in resultado} == {"nao_localizada"}
    assert len(_linhas(banco, proposta_id)) == 10


def test_prompts_dos_tres_papeis_cobrem_o_essencial():
    verificador = agentes.PROMPT_VERIFICADOR_FONTES
    for termo in ("web_fetch", "web_search", "registrar_verificacao", "licença", "periodicidade"):
        assert termo in verificador
    assert "exatamente uma vez" in verificador
    assert "fora do escopo" in agentes.PROMPT_REDATOR_PROJETO
    assert "lacuna" in agentes.PROMPT_CRITICO_PROJETO
