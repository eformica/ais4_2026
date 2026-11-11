-- =====================================================================
-- 10_execucao.sql  —  Motor de Execução de Projetos (seção 7)
-- Orçamento por projeto, tarefas (uma linha por tentativa de etapa) e o
-- log de chamadas ao LLM (somente inserção), com a view de consumo.
-- Depende de: projetos (projeto). A fila de tarefas é a própria tabela
-- (FOR UPDATE SKIP LOCKED + lease, decisão D7).
-- Grafo de dependências (sem ciclos): ... <- projetos <- execucao.
-- =====================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'execucao_owner') THEN
    CREATE ROLE execucao_owner NOLOGIN;
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS execucao AUTHORIZATION execucao_owner;
REVOKE ALL ON SCHEMA execucao FROM PUBLIC;

-- USAGE + REFERENCES só para declarar as FKs; não concede leitura de tabelas.
GRANT USAGE ON SCHEMA projetos TO execucao_owner;
GRANT REFERENCES ON projetos.projeto TO execucao_owner;

SET ROLE execucao_owner;

-- ---------------------------------------------------------------------
-- Utilitários do schema
-- ---------------------------------------------------------------------
CREATE FUNCTION execucao.autor_atual() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(NULLIF(current_setting('app.usuario', true), ''), current_user::text)
$$;

CREATE FUNCTION execucao.trg_somente_insercao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Tabela %.% aceita apenas inserções', TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'check_violation';
END $$;

-- ---------------------------------------------------------------------
-- Orçamento (um por projeto). prazo NULL = sem prazo.
-- ---------------------------------------------------------------------
CREATE TABLE execucao.orcamento (
  projeto_id     uuid          PRIMARY KEY REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  max_tokens     bigint        NOT NULL CHECK (max_tokens > 0),
  max_custo_usd  numeric(10,2) NOT NULL CHECK (max_custo_usd > 0),
  prazo          timestamptz
);

-- ---------------------------------------------------------------------
-- Tarefa: uma linha por tentativa de uma etapa. Reexecutar não apaga a
-- anterior; cria tentativa + 1.
-- ---------------------------------------------------------------------
CREATE TABLE execucao.tarefa (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  projeto_id     uuid        NOT NULL REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  etapa_codigo   text        NOT NULL,
  tentativa      smallint    NOT NULL CHECK (tentativa > 0),
  status         text        NOT NULL DEFAULT 'pronta'
                 CHECK (status IN ('pronta', 'em_execucao', 'validando', 'aguardando_gate',
                                   'concluida', 'falhou', 'cancelada')),
  papel          text        NOT NULL,
  modelo         text        NOT NULL,
  entradas       jsonb       NOT NULL DEFAULT '{}',
  saidas         jsonb,
  feedback       jsonb,
  erro           text,
  ambiente       jsonb,
  semente        int,
  lease_ate      timestamptz,
  iniciado_em    timestamptz,
  finalizado_em  timestamptz,
  criado_em      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (projeto_id, etapa_codigo, tentativa)
);

-- Lease vencido: o orquestrador procura tarefas em_execucao com lease_ate < now().
CREATE INDEX tarefa_lease_idx ON execucao.tarefa (lease_ate) WHERE status = 'em_execucao';

-- Ciclo de vida: pronta > em_execucao > validando > concluida | falhou | aguardando_gate;
-- aguardando_gate > concluida | falhou; em_execucao > falhou; qualquer não terminal > cancelada.
-- Terminais (concluida, falhou, cancelada) não mudam mais, nem saidas/entradas/feedback.
CREATE FUNCTION execucao.trg_tarefa_transicao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status IN ('concluida', 'falhou', 'cancelada') THEN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
      RAISE EXCEPTION 'Transição de status de tarefa inválida: % -> % (tarefa %): estado terminal',
        OLD.status, NEW.status, NEW.id
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.saidas   IS DISTINCT FROM OLD.saidas
       OR NEW.entradas IS DISTINCT FROM OLD.entradas
       OR NEW.feedback IS DISTINCT FROM OLD.feedback THEN
      RAISE EXCEPTION 'Tarefa % já terminou (%): saidas, entradas e feedback são imutáveis',
        NEW.id, OLD.status
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;

  IF NOT (OLD.status || '>' || NEW.status) = ANY (ARRAY[
       'pronta>em_execucao',
       'em_execucao>validando',
       'em_execucao>falhou',
       'validando>concluida',
       'validando>falhou',
       'validando>aguardando_gate',
       'aguardando_gate>concluida',
       'aguardando_gate>falhou',
       'pronta>cancelada',
       'em_execucao>cancelada',
       'validando>cancelada',
       'aguardando_gate>cancelada'
     ]) THEN
    RAISE EXCEPTION 'Transição de status de tarefa inválida: % -> % (tarefa %)',
      OLD.status, NEW.status, NEW.id
      USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status IN ('concluida', 'falhou', 'cancelada') THEN
    NEW.finalizado_em := COALESCE(NEW.finalizado_em, now());
  END IF;
  IF OLD.status = 'em_execucao' THEN
    NEW.lease_ate := NULL;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER tarefa_transicao
  BEFORE UPDATE ON execucao.tarefa
  FOR EACH ROW EXECUTE FUNCTION execucao.trg_tarefa_transicao();

-- ---------------------------------------------------------------------
-- Chamadas ao LLM (somente inserção). projeto_id é NULL nas chamadas do
-- Motor de Criação, que têm proposta_id (uuid lógico, sem FK: a proposta
-- vive em outro schema que não depende deste).
-- ---------------------------------------------------------------------
CREATE TABLE execucao.chamada_llm (
  id                    bigint        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tarefa_id             uuid          REFERENCES execucao.tarefa (id) ON DELETE RESTRICT,
  proposta_id           uuid,
  projeto_id            uuid          REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  modelo                text          NOT NULL,
  tokens_entrada        bigint        NOT NULL CHECK (tokens_entrada >= 0),
  tokens_saida          bigint        NOT NULL CHECK (tokens_saida >= 0),
  tokens_cache_leitura  bigint        NOT NULL DEFAULT 0 CHECK (tokens_cache_leitura >= 0),
  buscas_web            int           NOT NULL DEFAULT 0 CHECK (buscas_web >= 0),
  custo_usd             numeric(12,6) NOT NULL CHECK (custo_usd >= 0),
  duracao_ms            int           NOT NULL CHECK (duracao_ms >= 0),
  request_id            text,
  criado_em             timestamptz   NOT NULL DEFAULT now()
);

CREATE INDEX chamada_llm_projeto_idx ON execucao.chamada_llm (projeto_id) WHERE projeto_id IS NOT NULL;
CREATE INDEX chamada_llm_proposta_idx ON execucao.chamada_llm (proposta_id) WHERE proposta_id IS NOT NULL;
CREATE INDEX chamada_llm_tarefa_idx ON execucao.chamada_llm (tarefa_id) WHERE tarefa_id IS NOT NULL;

CREATE TRIGGER chamada_llm_somente_insercao
  BEFORE UPDATE OR DELETE ON execucao.chamada_llm
  FOR EACH ROW EXECUTE FUNCTION execucao.trg_somente_insercao();

-- Consumo por projeto. tokens = entrada + saída + leitura de cache (o que o
-- orçamento de tokens conta). Só chamadas com projeto_id.
CREATE VIEW execucao.consumo AS
SELECT projeto_id,
       sum(tokens_entrada + tokens_saida + tokens_cache_leitura)::bigint AS tokens,
       sum(custo_usd)::numeric(14,6)                                     AS custo_usd
  FROM execucao.chamada_llm
 WHERE projeto_id IS NOT NULL
 GROUP BY projeto_id;

-- ---------------------------------------------------------------------
-- Outbox
-- ---------------------------------------------------------------------
CREATE TABLE execucao.outbox (
  id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agregado_id   uuid        NOT NULL,
  tipo_evento   text        NOT NULL,
  payload       jsonb       NOT NULL DEFAULT '{}',
  criado_em     timestamptz NOT NULL DEFAULT now(),
  publicado_em  timestamptz
);
CREATE INDEX outbox_pendentes_idx ON execucao.outbox (criado_em) WHERE publicado_em IS NULL;

RESET ROLE;

-- ---------------------------------------------------------------------
-- Grants de execucao_app (mesmo padrão de 05/09): SELECT/INSERT/UPDATE, sem DELETE
-- ---------------------------------------------------------------------
GRANT USAGE ON SCHEMA execucao TO execucao_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA execucao TO execucao_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA execucao TO execucao_app;
ALTER DEFAULT PRIVILEGES FOR ROLE execucao_owner IN SCHEMA execucao
  GRANT SELECT, INSERT, UPDATE ON TABLES TO execucao_app;
ALTER DEFAULT PRIVILEGES FOR ROLE execucao_owner IN SCHEMA execucao
  GRANT USAGE, SELECT ON SEQUENCES TO execucao_app;
