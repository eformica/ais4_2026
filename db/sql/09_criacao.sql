-- =====================================================================
-- 09_criacao.sql  —  Motor de Criação de Projetos (seção 4.2)
-- Propostas (rascunho, autocrítica, gate), iterações da autocrítica e
-- verificação de fontes. Depende de: acervo (ideia), playbooks (versão),
-- projetos (projeto criado na aprovação).
-- Grafo de dependências (sem ciclos): playbooks <- acervo <- projetos <- criacao.
-- =====================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'criacao_owner') THEN
    CREATE ROLE criacao_owner NOLOGIN;
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS criacao AUTHORIZATION criacao_owner;
REVOKE ALL ON SCHEMA criacao FROM PUBLIC;

-- USAGE + REFERENCES só para declarar as FKs; não concede leitura de tabelas.
GRANT USAGE ON SCHEMA acervo, playbooks, projetos TO criacao_owner;
GRANT REFERENCES ON acervo.ideia              TO criacao_owner;
GRANT REFERENCES ON playbooks.playbook_versao TO criacao_owner;
GRANT REFERENCES ON projetos.projeto          TO criacao_owner;

SET ROLE criacao_owner;

-- ---------------------------------------------------------------------
-- Utilitários do schema
-- ---------------------------------------------------------------------
CREATE FUNCTION criacao.autor_atual() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(NULLIF(current_setting('app.usuario', true), ''), current_user::text)
$$;

CREATE FUNCTION criacao.trg_atualizado_em() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.atualizado_em := now();
  RETURN NEW;
END $$;

CREATE FUNCTION criacao.trg_somente_insercao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Tabela %.% aceita apenas inserções', TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'check_violation';
END $$;

-- ---------------------------------------------------------------------
-- Proposta
-- playbook_id/playbook_versao só são conhecidos depois do casamento com o
-- playbook; a reserva da ideia (índice único abaixo) vem antes.
-- ---------------------------------------------------------------------
CREATE TABLE criacao.proposta (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  ideia_id         uuid        NOT NULL REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  playbook_id      uuid,
  playbook_versao  text,
  status           text        NOT NULL DEFAULT 'rascunho'
                   CHECK (status IN ('rascunho', 'em_autocritica', 'aguardando_aprovacao',
                                     'ajustes_solicitados', 'aprovada', 'rejeitada',
                                     'devolvida')),
  conteudo         jsonb       CHECK (conteudo IS NULL OR jsonb_typeof(conteudo) = 'object'),
  iteracoes        smallint    NOT NULL DEFAULT 0 CHECK (iteracoes >= 0),
  motivo           text,
  solicitado_por   text        NOT NULL DEFAULT criacao.autor_atual(),
  decidido_por     text,
  decidido_em      timestamptz,
  projeto_id       uuid        REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  criado_em        timestamptz NOT NULL DEFAULT now(),
  atualizado_em    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (playbook_id, playbook_versao)
    REFERENCES playbooks.playbook_versao (playbook_id, versao) ON DELETE RESTRICT,
  CHECK ((playbook_id IS NULL) = (playbook_versao IS NULL)),
  -- só rascunho e devolvida podem existir sem playbook (R6)
  CHECK (status IN ('rascunho', 'devolvida') OR playbook_id IS NOT NULL),
  CHECK (status <> 'devolvida' OR motivo IS NOT NULL)
);

CREATE INDEX proposta_ideia_idx ON criacao.proposta (ideia_id);

-- Uma ideia tem no máximo uma proposta ativa: é o que faz dois motores
-- concorrentes não pegarem a mesma ideia.
CREATE UNIQUE INDEX proposta_ideia_ativa_uq ON criacao.proposta (ideia_id)
  WHERE status IN ('rascunho', 'em_autocritica', 'aguardando_aprovacao', 'ajustes_solicitados');

CREATE TRIGGER proposta_atualizado_em
  BEFORE UPDATE ON criacao.proposta
  FOR EACH ROW EXECUTE FUNCTION criacao.trg_atualizado_em();

-- ---------------------------------------------------------------------
-- Iterações da autocrítica (somente inserção)
-- ---------------------------------------------------------------------
CREATE TABLE criacao.proposta_iteracao (
  proposta_id            uuid          NOT NULL REFERENCES criacao.proposta (id) ON DELETE RESTRICT,
  n                      smallint      NOT NULL CHECK (n > 0),
  rascunho               jsonb         NOT NULL,
  lacunas                jsonb         NOT NULL DEFAULT '[]',
  aprovada_pela_critica  boolean       NOT NULL,
  custo_usd              numeric(12,6) NOT NULL DEFAULT 0,
  criado_em              timestamptz   NOT NULL DEFAULT now(),
  PRIMARY KEY (proposta_id, n)
);

CREATE TRIGGER proposta_iteracao_somente_insercao
  BEFORE UPDATE OR DELETE ON criacao.proposta_iteracao
  FOR EACH ROW EXECUTE FUNCTION criacao.trg_somente_insercao();

-- ---------------------------------------------------------------------
-- Verificação das fontes de dados (passo 3 do fluxo)
-- ---------------------------------------------------------------------
CREATE TABLE criacao.fonte_verificada (
  proposta_id  uuid        NOT NULL REFERENCES criacao.proposta (id) ON DELETE RESTRICT,
  nome         text        NOT NULL,
  url          text,
  status       text        NOT NULL
               CHECK (status IN ('acessivel', 'inacessivel', 'nao_localizada')),
  cobertura    jsonb       NOT NULL DEFAULT '{}',
  evidencia    text        NOT NULL,
  testado_em   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (proposta_id, nome)
);

-- ---------------------------------------------------------------------
-- Outbox
-- ---------------------------------------------------------------------
CREATE TABLE criacao.outbox (
  id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agregado_id   uuid        NOT NULL,
  tipo_evento   text        NOT NULL,
  payload       jsonb       NOT NULL DEFAULT '{}',
  criado_em     timestamptz NOT NULL DEFAULT now(),
  publicado_em  timestamptz
);
CREATE INDEX outbox_pendentes_idx ON criacao.outbox (criado_em) WHERE publicado_em IS NULL;

RESET ROLE;

-- ---------------------------------------------------------------------
-- Grants de criacao_app (mesmo padrão de 05): SELECT/INSERT/UPDATE, sem DELETE
-- ---------------------------------------------------------------------
GRANT USAGE ON SCHEMA criacao TO criacao_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA criacao TO criacao_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA criacao TO criacao_app;
ALTER DEFAULT PRIVILEGES FOR ROLE criacao_owner IN SCHEMA criacao
  GRANT SELECT, INSERT, UPDATE ON TABLES TO criacao_app;
ALTER DEFAULT PRIVILEGES FOR ROLE criacao_owner IN SCHEMA criacao
  GRANT USAGE, SELECT ON SEQUENCES TO criacao_app;
