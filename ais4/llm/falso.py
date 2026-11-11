"""`ClienteLLMFalso`: ClienteLLM determinístico para testes. Nenhuma chamada de rede."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from ais4.llm.cliente import ClienteLLM, RespostaInvalida, T, Uso
from ais4.llm.custos import calcular_custo


class RoteiroEsgotado(AssertionError):
    """O teste fez mais chamadas do que o roteiro previa para o papel."""


@dataclass
class ChamadaFalsa:
    metodo: Literal["estruturado", "agente"]
    papel: str
    sistema: str
    mensagem: str
    formato: type | None = None
    ferramentas: list[str] = field(default_factory=list)
    max_iteracoes: int | None = None
    # (nome, argumentos) de cada ferramenta que o roteiro invocou, em ordem.
    ferramentas_usadas: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def _uso_padrao() -> Uso:
    modelo = "claude-opus-5-5"
    return Uso(modelo, 100, 50, 0, 0, calcular_custo(modelo, 100, 50, 0, 0), 1, None)


def _nome(ferramenta: Any) -> str | None:
    if isinstance(ferramenta, dict):
        return ferramenta.get("name")
    return getattr(ferramenta, "name", None) or getattr(ferramenta, "__name__", None)


def _funcao(ferramenta: Any) -> Callable[..., Any] | None:
    """A função Python por trás de um `@beta_tool`; `None` para ferramentas de servidor."""
    if isinstance(ferramenta, dict):
        return None
    return getattr(ferramenta, "func", None) or (ferramenta if callable(ferramenta) else None)


class ClienteLLMFalso(ClienteLLM):
    """Roteiro: `{papel: [item, ...]}`; cada chamada do papel consome o próximo item.

    `estruturado` aceita como item:
      - instância do modelo Pydantic `formato` (devolvida);
      - `str` com JSON (validada como o cliente real: inválida -> `RespostaInvalida("schema")`);
      - exceção (levantada). Se for `RespostaInvalida`, o uso é reportado antes, como no
        cliente real (a chamada foi cobrada); qualquer outra exceção é levantada sem uso.

    `agente` aceita como item:
      - `str`: texto final, com UM relato de uso;
      - exceção: como acima;
      - `Callable[[dict[str, Callable]], str]`: simula o modelo. Recebe as ferramentas
        locais por nome (envolvidas) e devolve o texto final. Protocolo de iterações,
        espelhando o cliente real (uma mensagem do modelo = um `ao_usar`, antes das
        ferramentas dela):
          * cada chamada a `ferramentas[nome](**kwargs)` é uma mensagem do modelo que pede
            a ferramenta: o fake chama `ao_usar` PRIMEIRO e só então executa a função;
            se `ao_usar` levantar (`OrcamentoExcedido`), a ferramenta não roda e a exceção
            sobe pelo callable;
          * uma exceção da ferramenta vira o texto `"Error: <exceção>"` devolvido ao callable
            (como o runner devolve `is_error` ao modelo), sem derrubar o laço;
          * o retorno do callable é a mensagem final: um último `ao_usar`;
          * N chamadas de ferramenta => N+1 relatos de uso. Se a chamada de ferramenta de
            número `max_iteracoes` for feita, o fake reporta o uso, EXECUTA a ferramenta (como o
            runner real, que roda as ferramentas da mensagem que atinge o limite) e então
            levanta `RespostaInvalida("sem_conclusao")` (o laço real termina sem texto).

    Cada mensagem reporta o mesmo `Uso` (`uso=`; padrão: Opus 5.5, 100 entrada, 50 saída).
    `chamadas` guarda cada chamada recebida (`ChamadaFalsa`) para asserções.
    """

    def __init__(self, roteiro: dict[str, list[object]], *, uso: Uso | None = None) -> None:
        self._roteiro = {papel: list(itens) for papel, itens in roteiro.items()}
        self._uso = uso or _uso_padrao()
        self.chamadas: list[ChamadaFalsa] = []

    def _proximo(self, papel: str) -> object:
        itens = self._roteiro.get(papel)
        if not itens:
            raise RoteiroEsgotado(f"roteiro esgotado para o papel {papel!r}")
        return itens.pop(0)

    def _falhar(self, item: BaseException, ao_usar: Callable[[Uso], None]) -> None:
        if isinstance(item, RespostaInvalida):
            ao_usar(self._uso)
        raise item

    def estruturado(
        self,
        *,
        papel: str,
        sistema: str,
        mensagem: str,
        formato: type[T],
        ao_usar: Callable[[Uso], None],
    ) -> T:
        self.chamadas.append(ChamadaFalsa("estruturado", papel, sistema, mensagem, formato=formato))
        item = self._proximo(papel)
        if isinstance(item, BaseException):
            self._falhar(item, ao_usar)
        ao_usar(self._uso)
        if isinstance(item, str):
            try:
                return formato.model_validate_json(item)
            except ValidationError as erro:
                raise RespostaInvalida("schema", str(erro)) from erro
        if isinstance(item, formato):
            return item
        raise TypeError(
            f"item do roteiro de {papel!r} deveria ser {formato.__name__}, str ou exceção; "
            f"veio {type(item).__name__}"
        )

    def agente(
        self,
        *,
        papel: str,
        sistema: str,
        mensagem: str,
        ferramentas: list,
        ao_usar: Callable[[Uso], None],
        max_iteracoes: int = 40,
    ) -> str:
        chamada = ChamadaFalsa(
            "agente",
            papel,
            sistema,
            mensagem,
            ferramentas=[n for n in map(_nome, ferramentas) if n],
            max_iteracoes=max_iteracoes,
        )
        self.chamadas.append(chamada)
        item = self._proximo(papel)
        if isinstance(item, BaseException):
            self._falhar(item, ao_usar)
        if isinstance(item, str):
            ao_usar(self._uso)
            return item
        if isinstance(item, BaseModel) or not callable(item):
            raise TypeError(
                f"item do roteiro de agente {papel!r} deveria ser str, exceção ou callable; "
                f"veio {type(item).__name__}"
            )

        mensagens = 0

        def envolver(nome: str, funcao: Callable[..., Any]) -> Callable[..., Any]:
            def chamar(**kwargs: Any) -> Any:
                nonlocal mensagens
                mensagens += 1
                ao_usar(self._uso)
                chamada.ferramentas_usadas.append((nome, kwargs))
                try:
                    resultado = funcao(**kwargs)
                except Exception as erro:  # noqa: BLE001 - o runner real também devolve o erro
                    resultado = f"Error: {erro}"
                if mensagens >= max_iteracoes:
                    # Como o runner real: a mensagem que atinge o limite tem as ferramentas
                    # executadas, e então o laço para com a última mensagem ainda pedindo ferramenta.
                    raise RespostaInvalida(
                        "sem_conclusao", f"limite de {max_iteracoes} iterações atingido"
                    )
                return resultado

            return chamar

        locais = {}
        for ferramenta in ferramentas:
            nome, funcao = _nome(ferramenta), _funcao(ferramenta)
            if nome and funcao:
                locais[nome] = envolver(nome, funcao)

        texto = item(locais)
        ao_usar(self._uso)
        return texto
