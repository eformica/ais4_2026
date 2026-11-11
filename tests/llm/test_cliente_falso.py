from decimal import Decimal

import pytest
from anthropic import beta_tool
from pydantic import BaseModel

from ais4.llm.cliente import OrcamentoExcedido, RespostaInvalida, Uso
from ais4.llm.falso import ClienteLLMFalso, RoteiroEsgotado


class Saida(BaseModel):
    valor: int


@beta_tool
def soma(a: int, b: int) -> str:
    """Soma dois números.

    Args:
        a: primeiro
        b: segundo
    """
    return str(a + b)


@beta_tool
def quebra() -> str:
    """Sempre falha."""
    raise RuntimeError("boom")


def _estruturado(cliente, usos, papel="criador"):
    return cliente.estruturado(
        papel=papel, sistema="S", mensagem="M", formato=Saida, ao_usar=usos.append
    )


def _agente(cliente, usos, ferramentas, papel="coletor", **kw):
    return cliente.agente(
        papel=papel, sistema="S", mensagem="M", ferramentas=ferramentas, ao_usar=usos.append, **kw
    )


def test_falso_estruturado_reporta_uso():
    cliente = ClienteLLMFalso({"criador": [Saida(valor=7)]})
    usos: list[Uso] = []

    saida = _estruturado(cliente, usos)

    assert saida == Saida(valor=7)
    assert len(usos) == 1
    assert usos[0].modelo == "claude-opus-5-5"
    assert usos[0].custo_usd == Decimal("0.001400")  # 100 entrada + 50 saída no Opus 5.5
    assert cliente.chamadas[0].metodo == "estruturado"
    assert cliente.chamadas[0].papel == "criador"
    assert cliente.chamadas[0].formato is Saida
    assert cliente.chamadas[0].sistema == "S"
    assert cliente.chamadas[0].mensagem == "M"


def test_falso_uso_configuravel():
    uso = Uso("claude-sonnet-5-5", 1, 2, 3, 4, Decimal("9.5"), 11, "req_x")
    cliente = ClienteLLMFalso({"criador": [Saida(valor=1)]}, uso=uso)
    usos: list[Uso] = []

    _estruturado(cliente, usos)

    assert usos == [uso]


def test_falso_consome_roteiro_por_papel_em_ordem():
    cliente = ClienteLLMFalso({"a": [Saida(valor=1), Saida(valor=2)], "b": [Saida(valor=3)]})
    usos: list[Uso] = []

    assert _estruturado(cliente, usos, "a").valor == 1
    assert _estruturado(cliente, usos, "b").valor == 3
    assert _estruturado(cliente, usos, "a").valor == 2
    with pytest.raises(RoteiroEsgotado):
        _estruturado(cliente, usos, "a")
    with pytest.raises(RoteiroEsgotado):
        _estruturado(cliente, usos, "papel-sem-roteiro")


def test_falso_item_excecao_e_levantado():
    cliente = ClienteLLMFalso({"criador": [ValueError("falhou")]})
    usos: list[Uso] = []

    with pytest.raises(ValueError, match="falhou"):
        _estruturado(cliente, usos)
    assert usos == []


def test_resposta_invalida_carrega_motivo():
    erro = RespostaInvalida("refusal", "recusou")
    assert erro.motivo == "refusal"
    assert erro.detalhe == "recusou"
    assert "refusal" in str(erro) and "recusou" in str(erro)

    # No cliente real a chamada já foi cobrada: o falso reporta o uso e depois levanta.
    cliente = ClienteLLMFalso({"criador": [erro]})
    usos: list[Uso] = []
    with pytest.raises(RespostaInvalida) as info:
        _estruturado(cliente, usos)
    assert info.value.motivo == "refusal"
    assert len(usos) == 1


def test_falso_estruturado_str_invalida_vira_schema():
    cliente = ClienteLLMFalso({"criador": ['{"valor": 5}', '{"valor": "x"}']})
    usos: list[Uso] = []

    assert _estruturado(cliente, usos) == Saida(valor=5)
    with pytest.raises(RespostaInvalida) as info:
        _estruturado(cliente, usos)
    assert info.value.motivo == "schema"
    assert len(usos) == 2


def test_falso_agente_str_devolve_texto_e_reporta_uma_vez():
    cliente = ClienteLLMFalso({"coletor": ["pronto"]})
    usos: list[Uso] = []

    texto = _agente(cliente, usos, [soma], max_iteracoes=5)

    assert texto == "pronto"
    assert len(usos) == 1
    chamada = cliente.chamadas[0]
    assert chamada.metodo == "agente"
    assert chamada.ferramentas == ["soma"]
    assert chamada.max_iteracoes == 5


def test_falso_agente_callable_chama_ferramentas_pelo_nome():
    def roteiro(ferramentas):
        a = ferramentas["soma"](a=1, b=2)
        b = ferramentas["soma"](a=int(a), b=10)
        return f"total={b}"

    cliente = ClienteLLMFalso({"coletor": [roteiro]})
    usos: list[Uso] = []

    texto = _agente(cliente, usos, [soma, {"type": "web_search_20260209", "name": "web_search"}])

    assert texto == "total=13"
    # 2 chamadas de ferramenta => 3 mensagens do modelo => 3 relatos de uso.
    assert len(usos) == 3
    assert cliente.chamadas[0].ferramentas == ["soma", "web_search"]
    assert cliente.chamadas[0].ferramentas_usadas == [
        ("soma", {"a": 1, "b": 2}),
        ("soma", {"a": 3, "b": 10}),
    ]


def test_falso_agente_ao_usar_vem_antes_de_cada_ferramenta():
    ordem: list[str] = []

    @beta_tool
    def registra() -> str:
        """Registra."""
        ordem.append("ferramenta")
        return "ok"

    def roteiro(ferramentas):
        ferramentas["registra"]()
        return "fim"

    cliente = ClienteLLMFalso({"coletor": [roteiro]})
    cliente.agente(
        papel="coletor",
        sistema="S",
        mensagem="M",
        ferramentas=[registra],
        ao_usar=lambda uso: ordem.append("uso"),
    )

    assert ordem == ["uso", "ferramenta", "uso"]


def test_falso_agente_interrompido_por_orcamento():
    executadas: list[int] = []

    @beta_tool
    def trabalha(n: int) -> str:
        """Trabalha.

        Args:
            n: passo
        """
        executadas.append(n)
        return "ok"

    def roteiro(ferramentas):
        ferramentas["trabalha"](n=1)
        ferramentas["trabalha"](n=2)
        ferramentas["trabalha"](n=3)
        return "nunca"

    cliente = ClienteLLMFalso({"coletor": [roteiro]})
    recebidos: list[Uso] = []

    def ao_usar(uso: Uso) -> None:
        recebidos.append(uso)
        if len(recebidos) == 2:
            raise OrcamentoExcedido("estourou")

    with pytest.raises(OrcamentoExcedido):
        cliente.agente(
            papel="coletor", sistema="S", mensagem="M", ferramentas=[trabalha], ao_usar=ao_usar
        )

    # A 2ª mensagem do modelo estourou o orçamento: as ferramentas 2 e 3 não rodaram.
    assert executadas == [1]
    assert len(recebidos) == 2


def test_falso_agente_erro_de_ferramenta_vira_resultado_de_erro():
    def roteiro(ferramentas):
        return ferramentas["quebra"]()

    cliente = ClienteLLMFalso({"coletor": [roteiro]})
    usos: list[Uso] = []

    texto = _agente(cliente, usos, [quebra])

    assert texto.startswith("Error:") and "boom" in texto


def test_falso_agente_respeita_max_iteracoes():
    def roteiro(ferramentas):
        for _ in range(10):
            ferramentas["soma"](a=1, b=1)
        return "nunca"

    cliente = ClienteLLMFalso({"coletor": [roteiro]})
    usos: list[Uso] = []

    with pytest.raises(RespostaInvalida) as info:
        _agente(cliente, usos, [soma], max_iteracoes=3)

    assert info.value.motivo == "sem_conclusao"
    assert len(usos) == 3
    assert len(cliente.chamadas[0].ferramentas_usadas) == 2


def test_falso_agente_item_excecao():
    cliente = ClienteLLMFalso({"coletor": [RespostaInvalida("max_tokens", "cortou")]})
    usos: list[Uso] = []

    with pytest.raises(RespostaInvalida) as info:
        _agente(cliente, usos, [])

    assert info.value.motivo == "max_tokens"
    assert len(usos) == 1
