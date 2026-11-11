-- =====================================================================
-- 01_playbooks.sql  —  Base de Playbooks Metodológicos (seção 6)
-- Não depende de nenhum outro schema.
-- =====================================================================
SET ROLE playbooks_owner;

-- ---------------------------------------------------------------------
-- Utilitários do schema
-- ---------------------------------------------------------------------
CREATE FUNCTION playbooks.autor_atual() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(NULLIF(current_setting('app.usuario', true), ''), current_user::text)
$$;

-- ---------------------------------------------------------------------
-- Playbook (identidade estável)
-- ---------------------------------------------------------------------
CREATE TABLE playbooks.playbook (
  id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  codigo              text        NOT NULL UNIQUE CHECK (codigo ~ '^[a-z0-9][a-z0-9_-]*$'),
  nome                text        NOT NULL,
  descricao           text,
  classe_de_problema  text[]      NOT NULL DEFAULT '{}',
  criado_em           timestamptz NOT NULL DEFAULT now(),
  excluido_em         timestamptz
);

-- ---------------------------------------------------------------------
-- Versão do playbook (imutável depois de sair de 'proposto')
-- ---------------------------------------------------------------------
CREATE TABLE playbooks.playbook_versao (
  playbook_id          uuid        NOT NULL REFERENCES playbooks.playbook (id) ON DELETE RESTRICT,
  versao               text        NOT NULL CHECK (versao ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  status               text        NOT NULL DEFAULT 'proposto'
                       CHECK (status IN ('proposto', 'experimental', 'validado', 'depreciado')),
  pre_requisitos       jsonb       NOT NULL DEFAULT '[]',
  template_projeto     jsonb       NOT NULL DEFAULT '{}',
  criterios_qualidade  jsonb       NOT NULL DEFAULT '[]',
  entregaveis          jsonb       NOT NULL DEFAULT '[]',
  changelog            text,
  criado_em            timestamptz NOT NULL DEFAULT now(),
  publicado_em         timestamptz,
  PRIMARY KEY (playbook_id, versao),
  CHECK (jsonb_typeof(pre_requisitos)      = 'array'),
  CHECK (jsonb_typeof(template_projeto)    = 'object'),
  CHECK (jsonb_typeof(criterios_qualidade) = 'array'),
  CHECK (jsonb_typeof(entregaveis)         = 'array')
);

-- ---------------------------------------------------------------------
-- Etapa (nó do DAG de execução)
-- ---------------------------------------------------------------------
CREATE TABLE playbooks.etapa (
  playbook_id     uuid     NOT NULL,
  versao          text     NOT NULL,
  codigo          text     NOT NULL CHECK (codigo ~ '^[a-z0-9_]+$'),
  ordem           smallint NOT NULL CHECK (ordem > 0),
  nome            text     NOT NULL,
  descricao       text,
  depende_de      text[]   NOT NULL DEFAULT '{}',   -- códigos de etapas da mesma versão
  papel_agente    jsonb    NOT NULL DEFAULT '{}',   -- prompt-base, ferramentas permitidas
  entradas        jsonb    NOT NULL DEFAULT '[]',
  saidas          jsonb    NOT NULL DEFAULT '[]',
  criterio_saida  text     NOT NULL,
  exige_gate      boolean  NOT NULL DEFAULT false,
  papel_revisor   text,                             -- ex.: 'revisor_metodologico'
  PRIMARY KEY (playbook_id, versao, codigo),
  UNIQUE (playbook_id, versao, ordem),
  FOREIGN KEY (playbook_id, versao)
    REFERENCES playbooks.playbook_versao (playbook_id, versao) ON DELETE RESTRICT,
  CHECK (NOT exige_gate OR papel_revisor IS NOT NULL),
  CHECK (NOT (codigo = ANY (depende_de)))
);

-- ---------------------------------------------------------------------
-- Outbox de eventos do módulo
-- ---------------------------------------------------------------------
CREATE TABLE playbooks.outbox (
  id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agregado_id   uuid        NOT NULL,
  tipo_evento   text        NOT NULL,
  payload       jsonb       NOT NULL DEFAULT '{}',
  criado_em     timestamptz NOT NULL DEFAULT now(),
  publicado_em  timestamptz
);
CREATE INDEX outbox_pendentes_idx ON playbooks.outbox (criado_em) WHERE publicado_em IS NULL;

-- ---------------------------------------------------------------------
-- Regras: imutabilidade de versões publicadas e transições de status
-- ---------------------------------------------------------------------
CREATE FUNCTION playbooks.trg_versao_regras() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'proposto' THEN
      RAISE EXCEPTION 'Versão % do playbook % já publicada: use status depreciado em vez de apagar',
        OLD.versao, OLD.playbook_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN OLD;
  END IF;

  IF OLD.status <> 'proposto' AND (
       NEW.playbook_id         IS DISTINCT FROM OLD.playbook_id
    OR NEW.versao              IS DISTINCT FROM OLD.versao
    OR NEW.pre_requisitos      IS DISTINCT FROM OLD.pre_requisitos
    OR NEW.template_projeto    IS DISTINCT FROM OLD.template_projeto
    OR NEW.criterios_qualidade IS DISTINCT FROM OLD.criterios_qualidade
    OR NEW.entregaveis         IS DISTINCT FROM OLD.entregaveis
    OR NEW.changelog           IS DISTINCT FROM OLD.changelog) THEN
    RAISE EXCEPTION 'Versão % do playbook % é imutável; crie uma nova versão',
      OLD.versao, OLD.playbook_id USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
       (OLD.status || '>' || NEW.status) = ANY (ARRAY[
         'proposto>experimental',
         'experimental>validado',
         'proposto>depreciado',
         'experimental>depreciado',
         'validado>depreciado'])) THEN
    RAISE EXCEPTION 'Transição de status de playbook inválida: % -> %',
      OLD.status, NEW.status USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status <> 'proposto' AND NEW.publicado_em IS NULL THEN
    NEW.publicado_em := now();
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER versao_regras
  BEFORE UPDATE OR DELETE ON playbooks.playbook_versao
  FOR EACH ROW EXECUTE FUNCTION playbooks.trg_versao_regras();

CREATE FUNCTION playbooks.trg_versao_evento() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NULL;
  END IF;
  INSERT INTO playbooks.outbox (agregado_id, tipo_evento, payload)
  VALUES (NEW.playbook_id, 'PlaybookVersaoStatusAlterado',
          jsonb_build_object('versao', NEW.versao, 'de', OLD.status, 'para', NEW.status,
                             'autor', playbooks.autor_atual()));
  RETURN NULL;
END $$;

CREATE TRIGGER versao_evento
  AFTER UPDATE OF status ON playbooks.playbook_versao
  FOR EACH ROW EXECUTE FUNCTION playbooks.trg_versao_evento();

-- Etapas só podem ser criadas, alteradas ou apagadas enquanto a versão é 'proposto'
CREATE FUNCTION playbooks.trg_etapa_imutavel() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_status text;
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    SELECT status INTO v_status FROM playbooks.playbook_versao
     WHERE playbook_id = OLD.playbook_id AND versao = OLD.versao;
    IF v_status <> 'proposto' THEN
      RAISE EXCEPTION 'Etapas da versão % (status %) não podem ser alteradas',
        OLD.versao, v_status USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    SELECT status INTO v_status FROM playbooks.playbook_versao
     WHERE playbook_id = NEW.playbook_id AND versao = NEW.versao;
    IF v_status <> 'proposto' THEN
      RAISE EXCEPTION 'Etapas da versão % (status %) não podem ser alteradas',
        NEW.versao, v_status USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER etapa_imutavel
  BEFORE INSERT OR UPDATE OR DELETE ON playbooks.etapa
  FOR EACH ROW EXECUTE FUNCTION playbooks.trg_etapa_imutavel();

-- Valida que toda dependência aponta para uma etapa existente da mesma versão.
-- Chamar antes de promover uma versão de 'proposto' para 'experimental'.
CREATE FUNCTION playbooks.dependencias_invalidas(p_playbook_id uuid, p_versao text)
RETURNS TABLE (etapa text, dependencia_inexistente text)
LANGUAGE sql STABLE AS $$
  SELECT e.codigo, d.dep
    FROM playbooks.etapa e
    CROSS JOIN LATERAL unnest(e.depende_de) AS d(dep)
   WHERE e.playbook_id = p_playbook_id
     AND e.versao      = p_versao
     AND NOT EXISTS (SELECT 1 FROM playbooks.etapa x
                      WHERE x.playbook_id = e.playbook_id
                        AND x.versao      = e.versao
                        AND x.codigo      = d.dep)
$$;

-- ---------------------------------------------------------------------
-- Permissões para FKs vindas de outros módulos (REFERENCES, sem SELECT)
-- ---------------------------------------------------------------------
GRANT REFERENCES ON playbooks.playbook        TO acervo_owner, projetos_owner;
GRANT REFERENCES ON playbooks.playbook_versao TO projetos_owner;

RESET ROLE;
