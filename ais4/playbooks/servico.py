"""Serviço do módulo playbooks: sincronização classe -> banco, ciclo de vida e propostas."""

import logging
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ais4.auth import Usuario
from ais4.comum.dag import validar_dag
from ais4.comum.dto import VersaoPlaybookDTO
from ais4.comum.erros import NaoEncontrado, Proibido, RegraViolada
from ais4.db import sessao
from ais4.playbooks import registro
from ais4.playbooks.base import Etapa, PapelAgente, Playbook
from ais4.playbooks.modelos import (
    EtapaModelo,
    PlaybookModelo,
    PropostaMelhoriaModelo,
    VersaoModelo,
)

log = logging.getLogger(__name__)

PAPEL_GUARDIAO = "guardiao_de_playbooks"
STATUS_ATIVOS = ("experimental", "validado")
USUARIO_SISTEMA = "sistema"  # autor das leituras (só precisa de um nome para app.usuario)


# ---------------------------------------------------------------------------
# Serialização classe -> colunas. É a mesma usada para inserir e para comparar.
# ---------------------------------------------------------------------------
def _nulo_se_vazio(valor: str | None) -> str | None:
    return valor or None


def _colunas_playbook(pb: Playbook) -> dict:
    return {
        "codigo": pb.id,
        "nome": pb.nome,
        "descricao": _nulo_se_vazio(pb.descricao),
        "classe_de_problema": list(pb.classe_de_problema),
    }


def _colunas_versao(pb: Playbook) -> dict:
    return {
        "pre_requisitos": [{"tipo": r.tipo, "descricao": r.descricao} for r in pb.pre_requisitos],
        "template_projeto": pb.template_projeto,
        "criterios_qualidade": [
            {"codigo": c.codigo, "descricao": c.descricao} for c in pb.criterios_qualidade
        ],
        "entregaveis": [{"tipo": e.tipo} for e in pb.entregaveis],
        "changelog": _nulo_se_vazio(pb.changelog),
    }


def _colunas_etapa(e: Etapa) -> dict:
    return {
        "ordem": e.ordem,
        "nome": e.nome,
        "descricao": _nulo_se_vazio(e.descricao),
        "depende_de": list(e.depende_de),
        # R5: o prompt_base vive só na classe; o banco guarda papel e ferramentas.
        "papel_agente": {
            "papel": e.papel_agente.papel,
            "ferramentas": list(e.papel_agente.ferramentas),
        },
        "entradas": list(e.entradas),
        "saidas": list(e.saidas),
        "criterio_saida": e.criterio_saida,
        "exige_gate": e.exige_gate,
        "papel_revisor": e.papel_revisor,
    }


def _diferencas(linha, esperado: dict, prefixo: str = "") -> list[str]:
    # Comparação semântica: o driver devolve jsonb já parseado, então a ordem das chaves
    # não importa.
    return [
        f"{prefixo}{campo}" for campo, valor in esperado.items() if getattr(linha, campo) != valor
    ]


# ---------------------------------------------------------------------------
# Sincronização
# ---------------------------------------------------------------------------
def sincronizar(pb: Playbook, usuario: str) -> None:
    """Insere playbook/versão/etapas se a versão ainda não existe; se existe, exige igualdade.

    Qualquer diferença entre a classe e o que está no banco levanta
    `RegraViolada("versão publicada difere da classe")`; os campos divergentes vão para o log.
    """
    with sessao("playbooks", usuario) as s:
        playbook = s.scalars(select(PlaybookModelo).where(PlaybookModelo.codigo == pb.id)).first()
        if playbook is None:
            playbook = PlaybookModelo(**_colunas_playbook(pb))
            s.add(playbook)
            s.flush()
        versao = s.get(VersaoModelo, (playbook.id, pb.versao))
        if versao is None:
            _inserir_versao(s, playbook.id, pb)
            return

        diffs = _diferencas(playbook, _colunas_playbook(pb), "playbook.")
        diffs += _diferencas(versao, _colunas_versao(pb), "versao.")
        diffs += _diferencas_etapas(s, playbook.id, pb)
        if diffs:
            log.warning("%s %s difere da classe em: %s", pb.id, pb.versao, ", ".join(diffs))
            raise RegraViolada("versão publicada difere da classe")


def _inserir_versao(s: Session, playbook_id: UUID, pb: Playbook) -> None:
    s.add(VersaoModelo(playbook_id=playbook_id, versao=pb.versao, **_colunas_versao(pb)))
    s.flush()  # a versão precisa existir antes das etapas (FK e trigger de imutabilidade)
    s.add_all(
        EtapaModelo(playbook_id=playbook_id, versao=pb.versao, codigo=e.codigo, **_colunas_etapa(e))
        for e in pb.etapas
    )
    s.flush()


def _diferencas_etapas(s: Session, playbook_id: UUID, pb: Playbook) -> list[str]:
    linhas = {
        e.codigo: e
        for e in s.scalars(
            select(EtapaModelo).where(
                EtapaModelo.playbook_id == playbook_id, EtapaModelo.versao == pb.versao
            )
        )
    }
    da_classe = {e.codigo: e for e in pb.etapas}
    diffs = [f"etapa {c} ausente no banco" for c in da_classe.keys() - linhas.keys()]
    diffs += [f"etapa {c} ausente na classe" for c in linhas.keys() - da_classe.keys()]
    for codigo in da_classe.keys() & linhas.keys():
        diffs += _diferencas(linhas[codigo], _colunas_etapa(da_classe[codigo]), f"etapa {codigo}.")
    return diffs


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------
def _dtos_de_versoes(s: Session, *filtros) -> list[VersaoPlaybookDTO]:
    consulta = (
        select(
            VersaoModelo.playbook_id,
            PlaybookModelo.codigo,
            VersaoModelo.versao,
            VersaoModelo.status,
        )
        .join(PlaybookModelo, PlaybookModelo.id == VersaoModelo.playbook_id)
        .where(PlaybookModelo.excluido_em.is_(None), *filtros)
        .order_by(PlaybookModelo.codigo, VersaoModelo.versao)
    )
    return [VersaoPlaybookDTO(*linha) for linha in s.execute(consulta)]


def listar_versoes() -> list[VersaoPlaybookDTO]:
    """Todas as versões de todos os playbooks, em qualquer status."""
    with sessao("playbooks", USUARIO_SISTEMA) as s:
        return _dtos_de_versoes(s)


def versoes_ativas() -> list[VersaoPlaybookDTO]:
    """Versões `experimental` ou `validado`: as que o Motor de Criação pode usar."""
    with sessao("playbooks", USUARIO_SISTEMA) as s:
        return _dtos_de_versoes(s, VersaoModelo.status.in_(STATUS_ATIVOS))


def obter_versao(codigo: str, versao: str) -> VersaoPlaybookDTO:
    with sessao("playbooks", USUARIO_SISTEMA) as s:
        dtos = _dtos_de_versoes(s, PlaybookModelo.codigo == codigo, VersaoModelo.versao == versao)
    if not dtos:
        raise NaoEncontrado(f"Playbook {codigo} versão {versao} não encontrado")
    return dtos[0]


def _prompts_registrados(codigo: str, versao: str) -> dict[str, str]:
    try:
        pb = registro.obter(codigo, versao)
    except NaoEncontrado:
        return {}
    return {e.codigo: e.papel_agente.prompt_base for e in pb.etapas}


def _etapa_de_linha(linha: EtapaModelo, prompt_base: str) -> Etapa:
    papel = linha.papel_agente
    return Etapa(
        codigo=linha.codigo,
        ordem=linha.ordem,
        nome=linha.nome,
        descricao=linha.descricao,
        depende_de=tuple(linha.depende_de),
        papel_agente=PapelAgente(
            papel=papel.get("papel", ""),
            prompt_base=prompt_base,
            ferramentas=tuple(papel.get("ferramentas", ())),
        ),
        entradas=tuple(linha.entradas),
        saidas=tuple(linha.saidas),
        criterio_saida=linha.criterio_saida,
        exige_gate=linha.exige_gate,
        papel_revisor=linha.papel_revisor,
    )


def etapas(playbook_id: UUID, versao: str) -> list[Etapa]:
    """Etapas da versão, por `ordem`.

    O banco não guarda o `prompt_base` (R5): ele é preenchido a partir da classe registrada
    em `registro` e fica vazio quando a classe dessa versão não está registrada.
    """
    with sessao("playbooks", USUARIO_SISTEMA) as s:
        codigo = s.scalar(
            select(PlaybookModelo.codigo)
            .where(PlaybookModelo.id == playbook_id, VersaoModelo.versao == versao)
            .join(VersaoModelo, VersaoModelo.playbook_id == PlaybookModelo.id)
        )
        if codigo is None:
            raise NaoEncontrado(f"Versão {versao} do playbook {playbook_id} não encontrada")
        prompts = _prompts_registrados(codigo, versao)
        linhas = s.scalars(
            select(EtapaModelo)
            .where(EtapaModelo.playbook_id == playbook_id, EtapaModelo.versao == versao)
            .order_by(EtapaModelo.ordem)
        )
        return [_etapa_de_linha(linha, prompts.get(linha.codigo, "")) for linha in linhas]


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------
def alterar_status(playbook_id: UUID, versao: str, novo: str, usuario: Usuario) -> None:
    """Muda o status da versão (quem propôs não promove; só guardião de playbooks).

    Antes de `proposto -> experimental` exige dependências válidas e DAG sem ciclos.
    As transições permitidas são garantidas pelo trigger do banco.
    """
    if PAPEL_GUARDIAO not in usuario.papeis:
        raise Proibido(f"Requer o papel {PAPEL_GUARDIAO!r}")
    with sessao("playbooks", usuario.nome) as s:
        linha = s.scalars(
            select(VersaoModelo)
            .where(VersaoModelo.playbook_id == playbook_id, VersaoModelo.versao == versao)
            .with_for_update()
        ).first()
        if linha is None:
            raise NaoEncontrado(f"Versão {versao} do playbook {playbook_id} não encontrada")
        if usuario.nome == linha.proposto_por:
            raise Proibido("Quem propôs a versão não pode alterar o seu status")
        if linha.status == "proposto" and novo == "experimental":
            _validar_dag_do_banco(s, playbook_id, versao)
        linha.status = novo
        s.flush()  # o trigger do banco valida a transição; erros viram RegraViolada


def _validar_dag_do_banco(s: Session, playbook_id: UUID, versao: str) -> None:
    invalidas = s.execute(
        text("select etapa, dependencia_inexistente from playbooks.dependencias_invalidas(:p, :v)"),
        {"p": playbook_id, "v": versao},
    ).all()
    if invalidas:
        detalhe = ", ".join(f"{etapa} -> {dep}" for etapa, dep in invalidas)
        raise RegraViolada(f"Dependências para etapas inexistentes: {detalhe}")
    linhas = s.scalars(
        select(EtapaModelo).where(
            EtapaModelo.playbook_id == playbook_id, EtapaModelo.versao == versao
        )
    )
    validar_dag([_etapa_de_linha(linha, "") for linha in linhas])


# ---------------------------------------------------------------------------
# Propostas de melhoria (processo 2.2)
# ---------------------------------------------------------------------------
def registrar_proposta_melhoria(
    playbook_id: UUID,
    versao_base: str,
    descricao: str,
    origem_licao_id: UUID | None,
    origem_projeto_id: UUID | None,
    usuario: str,
) -> UUID:
    """Registra a proposta; idempotente por `origem_licao_id` (devolve o id já existente)."""
    valores = {
        "playbook_id": playbook_id,
        "versao_base": versao_base,
        "descricao": descricao,
        "origem_licao_id": origem_licao_id,
        "origem_projeto_id": origem_projeto_id,
    }
    with sessao("playbooks", usuario) as s:
        tabela = PropostaMelhoriaModelo.__table__
        novo_id = s.scalar(
            pg_insert(tabela)
            .values(**valores)
            .on_conflict_do_nothing(
                index_elements=["origem_licao_id"],
                index_where=tabela.c.origem_licao_id.is_not(None),
            )
            .returning(tabela.c.id)
        )
        if novo_id is not None:
            return novo_id
        return s.execute(
            select(tabela.c.id).where(tabela.c.origem_licao_id == origem_licao_id)
        ).scalar_one()
