"""ClienteAnthropic contra um transporte httpx falso: nenhuma chamada de rede real."""

import json
from decimal import Decimal

import anthropic
import httpx2
import pytest
from anthropic import beta_tool
from pydantic import BaseModel

from ais4.config import Config
from ais4.llm.cliente import ClienteAnthropic, OrcamentoExcedido, RespostaInvalida, Uso


class Saida(BaseModel):
    valor: int


def _mensagem(texto="", stop_reason="end_turn", usage=None, content=None, **extra):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content if content is not None else [{"type": "text", "text": texto}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 1000, "output_tokens": 500},
        **extra,
    }


def _sse(mensagem: dict) -> str:
    """Serializa uma mensagem completa como o fluxo de eventos da API."""
    inicio = {**mensagem, "content": [], "stop_reason": None}
    inicio["usage"] = {**mensagem["usage"], "output_tokens": 0}
    eventos = [("message_start", {"type": "message_start", "message": inicio})]
    for i, bloco in enumerate(mensagem["content"]):
        if bloco["type"] == "text":
            vazio = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": bloco["text"]}
        else:
            vazio = {**bloco, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(bloco["input"])}
        eventos.append(
            (
                "content_block_start",
                {"type": "content_block_start", "index": i, "content_block": vazio},
            )
        )
        eventos.append(
            ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta})
        )
        eventos.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    final = {
        "type": "message_delta",
        "delta": {
            "stop_reason": mensagem["stop_reason"],
            "stop_sequence": None,
            **({"stop_details": mensagem["stop_details"]} if "stop_details" in mensagem else {}),
        },
        "usage": mensagem["usage"],
    }
    eventos.append(("message_delta", final))
    eventos.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {nome}\ndata: {json.dumps(dado)}\n\n" for nome, dado in eventos)


class Servidor:
    """Responde em ordem às requisições e as guarda para inspeção."""

    def __init__(self, *respostas: dict, stream: bool = False):
        self.respostas = list(respostas)
        self.stream = stream
        self.requisicoes: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requisicoes.append(request)
        mensagem = self.respostas.pop(0)
        cabecalhos = {"request-id": f"req_{len(self.requisicoes)}"}
        if self.stream:
            return httpx2.Response(
                200,
                headers={**cabecalhos, "content-type": "text/event-stream"},
                content=_sse(mensagem).encode(),
            )
        return httpx2.Response(200, headers=cabecalhos, json=mensagem)

    def corpo(self, i: int = 0) -> dict:
        return json.loads(self.requisicoes[i].content)


def _cliente(servidor: Servidor, **config) -> ClienteAnthropic:
    http = httpx2.Client(transport=httpx2.MockTransport(servidor))
    sdk = anthropic.Anthropic(api_key="teste", http_client=http, max_retries=0)
    return ClienteAnthropic(sdk, Config(**config))


def _estruturado(cliente: ClienteAnthropic, usos: list[Uso], papel="criador"):
    return cliente.estruturado(
        papel=papel, sistema="SIS", mensagem="MSG", formato=Saida, ao_usar=usos.append
    )


def test_estruturado_devolve_objeto_validado_e_uso():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 2000,
        "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 0},
    }
    servidor = Servidor(_mensagem('{"valor": 42}', usage=usage))
    usos: list[Uso] = []

    saida = _estruturado(_cliente(servidor), usos)

    assert saida == Saida(valor=42)
    assert len(usos) == 1
    uso = usos[0]
    assert (uso.modelo, uso.tokens_entrada, uso.tokens_saida) == ("claude-opus-5-5", 1000, 500)
    assert (uso.tokens_cache_leitura, uso.buscas_web) == (2000, 2)
    # 1000*4/1e6 + 500*20/1e6 + 2000*0.2/1e6 + 2 buscas * 0.01
    assert uso.custo_usd == Decimal("0.034400")
    assert uso.request_id == "req_1"
    assert uso.duracao_ms >= 0


def test_estruturado_envia_parametros_exigidos():
    servidor = Servidor(_mensagem('{"valor": 1}'))
    cliente = _cliente(
        servidor,
        modelo_por_papel={"criador": "claude-sonnet-5-5"},
        effort_por_papel={"criador": "low"},
    )

    _estruturado(cliente, [])

    corpo = servidor.corpo()
    assert corpo["model"] == "claude-sonnet-5-5"
    assert corpo["max_tokens"] == 16000
    assert corpo["system"] == "SIS"
    assert corpo["messages"] == [{"role": "user", "content": "MSG"}]
    assert corpo["thinking"] == {"type": "adaptive"}
    assert corpo["output_config"]["effort"] == "low"
    assert corpo["output_config"]["format"]["type"] == "json_schema"
    assert corpo["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in servidor.requisicoes[0].headers["anthropic-beta"]
    assert "tool_choice" not in corpo


def test_estruturado_usa_padroes_para_papel_sem_configuracao():
    servidor = Servidor(_mensagem('{"valor": 1}'))

    _estruturado(_cliente(servidor), [], papel="qualquer")

    corpo = servidor.corpo()
    assert corpo["model"] == "claude-opus-5-5"
    assert corpo["output_config"]["effort"] == "high"


def test_estruturado_refusal_vira_resposta_invalida():
    servidor = Servidor(
        _mensagem(
            "",
            stop_reason="refusal",
            content=[],
            stop_details={"type": "refusal", "category": "cyber", "explanation": "nao posso"},
        )
    )
    usos: list[Uso] = []

    with pytest.raises(RespostaInvalida) as info:
        _estruturado(_cliente(servidor), usos)

    assert info.value.motivo == "refusal"
    assert "cyber" in info.value.detalhe
    # A chamada foi cobrada mesmo recusada: o uso foi reportado antes de levantar.
    assert len(usos) == 1


def test_estruturado_refusal_com_texto_nao_vira_erro_de_schema():
    """O stop_reason é checado antes de ler o conteúdo: texto livre não é validado."""
    servidor = Servidor(_mensagem("Não posso ajudar com isso.", stop_reason="refusal"))

    with pytest.raises(RespostaInvalida) as info:
        _estruturado(_cliente(servidor), [])

    assert info.value.motivo == "refusal"


def test_estruturado_max_tokens_com_json_truncado_e_max_tokens_nao_schema():
    servidor = Servidor(_mensagem('{"valor": 4', stop_reason="max_tokens"))

    with pytest.raises(RespostaInvalida) as info:
        _estruturado(_cliente(servidor), [])

    assert info.value.motivo == "max_tokens"


def test_estruturado_json_fora_do_schema():
    servidor = Servidor(_mensagem('{"valor": "nao-e-int"}'))
    usos: list[Uso] = []

    with pytest.raises(RespostaInvalida) as info:
        _estruturado(_cliente(servidor), usos)

    assert info.value.motivo == "schema"
    assert len(usos) == 1


def test_estruturado_sem_texto_e_sem_conclusao():
    pensamento = [{"type": "thinking", "thinking": "hm", "signature": "s"}]
    servidor = Servidor(_mensagem(content=pensamento))

    with pytest.raises(RespostaInvalida) as info:
        _estruturado(_cliente(servidor), [])

    assert info.value.motivo == "sem_conclusao"


def test_estruturado_orcamento_excedido_propaga():
    servidor = Servidor(_mensagem('{"valor": 1}'))

    def ao_usar(uso: Uso) -> None:
        raise OrcamentoExcedido("sem verba")

    with pytest.raises(OrcamentoExcedido):
        _cliente(servidor).estruturado(
            papel="criador", sistema="S", mensagem="M", formato=Saida, ao_usar=ao_usar
        )


def _tool_use(a=1, b=2):
    return [{"type": "tool_use", "id": "toolu_1", "name": "soma", "input": {"a": a, "b": b}}]


def _ferramenta_soma(executadas: list):
    @beta_tool
    def soma(a: int, b: int) -> str:
        """Soma dois números.

        Args:
            a: primeiro
            b: segundo
        """
        executadas.append((a, b))
        return str(a + b)

    return soma


def _agente(cliente: ClienteAnthropic, usos: list[Uso], ferramentas, **kw):
    return cliente.agente(
        papel="coletor",
        sistema="SIS",
        mensagem="MSG",
        ferramentas=ferramentas,
        ao_usar=usos.append,
        **kw,
    )


def test_agente_executa_ferramenta_e_devolve_texto_final():
    executadas: list = []
    servidor = Servidor(
        _mensagem(content=_tool_use(), stop_reason="tool_use"),
        _mensagem("total 3", usage={"input_tokens": 2000, "output_tokens": 100}),
        stream=True,
    )
    usos: list[Uso] = []

    texto = _agente(_cliente(servidor), usos, [_ferramenta_soma(executadas)])

    assert texto == "total 3"
    assert executadas == [(1, 2)]
    assert len(usos) == 2
    assert [u.tokens_entrada for u in usos] == [1000, 2000]
    assert [u.request_id for u in usos] == ["req_1", "req_2"]
    corpo = servidor.corpo(0)
    assert corpo["max_tokens"] == 64000
    assert corpo["stream"] is True
    assert corpo["thinking"] == {"type": "adaptive"}
    assert corpo["output_config"] == {"effort": "high"}
    assert corpo["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in servidor.requisicoes[0].headers["anthropic-beta"]
    assert corpo["tools"][0]["name"] == "soma"
    # O resultado da ferramenta volta ao modelo na 2ª requisição.
    ultimo = servidor.corpo(1)["messages"][-1]
    assert ultimo["role"] == "user"
    assert ultimo["content"][0]["type"] == "tool_result"
    assert "3" in json.dumps(ultimo["content"][0]["content"])


def test_agente_ao_usar_levanta_antes_da_ferramenta_e_da_proxima_chamada():
    executadas: list = []
    servidor = Servidor(
        _mensagem(content=_tool_use(), stop_reason="tool_use"),
        _mensagem("nunca"),
        stream=True,
    )

    def ao_usar(uso: Uso) -> None:
        raise OrcamentoExcedido("estourou")

    with pytest.raises(OrcamentoExcedido):
        _cliente(servidor).agente(
            papel="coletor",
            sistema="S",
            mensagem="M",
            ferramentas=[_ferramenta_soma(executadas)],
            ao_usar=ao_usar,
        )

    assert executadas == []
    assert len(servidor.requisicoes) == 1


def test_agente_erro_de_ferramenta_vai_ao_modelo_sem_derrubar_o_laco():
    @beta_tool
    def soma(a: int, b: int) -> str:
        """Soma.

        Args:
            a: primeiro
            b: segundo
        """
        raise RuntimeError("falha da ferramenta")

    servidor = Servidor(
        _mensagem(content=_tool_use(), stop_reason="tool_use"),
        _mensagem("me recuperei"),
        stream=True,
    )

    texto = _agente(_cliente(servidor), [], [soma])

    assert texto == "me recuperei"
    resultado = servidor.corpo(1)["messages"][-1]["content"][0]
    assert resultado["is_error"] is True
    assert "falha da ferramenta" in json.dumps(resultado["content"])


def test_agente_refusal_final_vira_resposta_invalida():
    servidor = Servidor(_mensagem("", stop_reason="refusal", content=[]), stream=True)
    usos: list[Uso] = []

    with pytest.raises(RespostaInvalida) as info:
        _agente(_cliente(servidor), usos, [])

    assert info.value.motivo == "refusal"
    assert len(usos) == 1


def test_agente_max_tokens_final_vira_resposta_invalida():
    servidor = Servidor(_mensagem("cortad", stop_reason="max_tokens"), stream=True)

    with pytest.raises(RespostaInvalida) as info:
        _agente(_cliente(servidor), [], [])

    assert info.value.motivo == "max_tokens"


def test_agente_sem_conclusao_quando_o_limite_de_iteracoes_corta_uma_chamada_de_ferramenta():
    servidor = Servidor(_mensagem(content=_tool_use(), stop_reason="tool_use"), stream=True)

    with pytest.raises(RespostaInvalida) as info:
        _agente(_cliente(servidor), [], [_ferramenta_soma([])], max_iteracoes=1)

    assert info.value.motivo == "sem_conclusao"
    assert len(servidor.requisicoes) == 1


def test_agente_conta_buscas_web_do_servidor():
    usage = {
        "input_tokens": 10,
        "output_tokens": 10,
        "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 1},
    }
    servidor = Servidor(_mensagem("ok", usage=usage), stream=True)
    usos: list[Uso] = []

    _agente(_cliente(servidor), usos, [])

    assert usos[0].buscas_web == 3
