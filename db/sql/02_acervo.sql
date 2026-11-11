-- =====================================================================
-- 02_acervo.sql  —  Acervo de Ideias e Problemas (seção 3)
-- Depende de: playbooks (FK opcional para playbook sugerido).
-- Não aponta para projetos: o vínculo "ideia gerada por retroalimentação"
-- fica em projetos.proximo_passo, evitando ciclo acervo <-> projetos.
-- =====================================================================
SET ROLE acervo_owner;

-- ---------------------------------------------------------------------
-- Utilitários do schema
-- ---------------------------------------------------------------------
CREATE FUNCTION acervo.autor_atual() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(NULLIF(current_setting('app.usuario', true), ''), current_user::text)
$$;

CREATE FUNCTION acervo.trg_atualizado_em() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.atualizado_em := now();
  RETURN NEW;
END $$;

CREATE FUNCTION acervo.trg_somente_insercao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Tabela %.% aceita apenas inserções', TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'check_violation';
END $$;

-- ---------------------------------------------------------------------
-- Ideia (registro 3.1)
-- ---------------------------------------------------------------------
CREATE TABLE acervo.ideia (
  id                    uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
  titulo                varchar(100) NOT NULL,
  tipo                  text         NOT NULL
                        CHECK (tipo IN ('problema', 'oportunidade', 'hipotese', 'pergunta_de_pesquisa')),
  descricao             text         NOT NULL,
  pergunta_norteadora   text         NOT NULL,
  dominio               text[]       NOT NULL CHECK (cardinality(dominio) > 0),
  origem                text         NOT NULL
                        CHECK (origem IN ('humana', 'retroalimentacao', 'fonte_externa')),
  origem_url            text,
  playbook_sugerido_id  uuid         REFERENCES playbooks.playbook (id) ON DELETE RESTRICT,
  fontes_candidatas     jsonb        NOT NULL DEFAULT '[]'
                        CHECK (jsonb_typeof(fontes_candidatas) = 'array'),
  restricoes            text,
  valor                 smallint     NOT NULL CHECK (valor       BETWEEN 1 AND 5),
  viabilidade           smallint     NOT NULL CHECK (viabilidade BETWEEN 1 AND 5),
  prioridade_manual     numeric(5,2),
  prioridade            numeric(5,2) GENERATED ALWAYS AS
                        (COALESCE(prioridade_manual, (valor * viabilidade)::numeric)) STORED,
  status                text         NOT NULL DEFAULT 'rascunho'
                        CHECK (status IN ('rascunho', 'triagem', 'qualificada', 'em_projeto',
                                          'concluida', 'arquivada', 'fundida')),
  motivo_status         text,
  fundida_em_id         uuid         REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  criado_por            text         NOT NULL DEFAULT acervo.autor_atual(),
  criado_em             timestamptz  NOT NULL DEFAULT now(),
  atualizado_em         timestamptz  NOT NULL DEFAULT now(),
  excluido_em           timestamptz,

  CHECK (origem <> 'fonte_externa' OR origem_url IS NOT NULL),
  CHECK (status <> 'arquivada' OR motivo_status IS NOT NULL),
  CHECK ((status = 'fundida') = (fundida_em_id IS NOT NULL)),
  CHECK (fundida_em_id IS DISTINCT FROM id),
  -- Critério 3.3 (2): ideia qualificada tem ao menos uma fonte candidata
  CHECK (status NOT IN ('qualificada', 'em_projeto', 'concluida')
         OR jsonb_array_length(fontes_candidatas) > 0)
);

CREATE INDEX ideia_fila_idx    ON acervo.ideia (status, prioridade DESC) WHERE excluido_em IS NULL;
CREATE INDEX ideia_dominio_idx ON acervo.ideia USING gin (dominio);

-- ---------------------------------------------------------------------
-- Relações entre ideias (duplicadas, filhas, dependentes)
-- ---------------------------------------------------------------------
CREATE TABLE acervo.ideia_relacao (
  ideia_id        uuid        NOT NULL REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  relacionada_id  uuid        NOT NULL REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  tipo            text        NOT NULL CHECK (tipo IN ('duplicada', 'filha', 'dependente', 'relacionada')),
  criado_em       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ideia_id, relacionada_id, tipo),
  CHECK (ideia_id <> relacionada_id)
);

-- ---------------------------------------------------------------------
-- Histórico (somente inserção)
-- ---------------------------------------------------------------------
CREATE TABLE acervo.ideia_evento (
  id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ideia_id     uuid        NOT NULL REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  evento       text        NOT NULL,
  de_status    text,
  para_status  text,
  motivo       text,
  autor        text        NOT NULL,
  ocorrido_em  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ideia_evento_ideia_idx ON acervo.ideia_evento (ideia_id, ocorrido_em);

CREATE TRIGGER ideia_evento_somente_insercao
  BEFORE UPDATE OR DELETE ON acervo.ideia_evento
  FOR EACH ROW EXECUTE FUNCTION acervo.trg_somente_insercao();

-- ---------------------------------------------------------------------
-- Outbox de eventos do módulo
-- ---------------------------------------------------------------------
CREATE TABLE acervo.outbox (
  id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agregado_id   uuid        NOT NULL,
  tipo_evento   text        NOT NULL,
  payload       jsonb       NOT NULL DEFAULT '{}',
  criado_em     timestamptz NOT NULL DEFAULT now(),
  publicado_em  timestamptz
);
CREATE INDEX outbox_pendentes_idx ON acervo.outbox (criado_em) WHERE publicado_em IS NULL;

-- ---------------------------------------------------------------------
-- Regras de ciclo de vida (3.2)
-- ---------------------------------------------------------------------
CREATE FUNCTION acervo.trg_ideia_transicao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;

  -- arquivada e fundida podem ser alcançadas de qualquer estado não terminal
  IF NEW.status IN ('arquivada', 'fundida')
     AND OLD.status NOT IN ('concluida', 'arquivada', 'fundida') THEN
    RETURN NEW;
  END IF;

  IF NOT (OLD.status || '>' || NEW.status) = ANY (ARRAY[
       'rascunho>triagem',
       'triagem>rascunho',        -- devolvida ao autor
       'triagem>qualificada',
       'qualificada>triagem',     -- devolvida (ex.: sem playbook compatível)
       'qualificada>em_projeto',
       'em_projeto>qualificada',  -- projeto cancelado, ideia volta à fila
       'em_projeto>concluida',
       'arquivada>triagem'        -- reaberta pelo processo 1.2
     ]) THEN
    RAISE EXCEPTION 'Transição de status de ideia inválida: % -> %', OLD.status, NEW.status
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER ideia_transicao
  BEFORE UPDATE OF status ON acervo.ideia
  FOR EACH ROW EXECUTE FUNCTION acervo.trg_ideia_transicao();

CREATE TRIGGER ideia_atualizado_em
  BEFORE UPDATE ON acervo.ideia
  FOR EACH ROW EXECUTE FUNCTION acervo.trg_atualizado_em();

CREATE FUNCTION acervo.trg_ideia_registrar() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_de     text;
  v_evento text := 'criada';
  v_tipo   text := 'IdeiaCriada';
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
      RETURN NULL;
    END IF;
    v_de     := OLD.status;
    v_evento := 'status_alterado';
    v_tipo   := 'IdeiaStatusAlterado';
  END IF;

  INSERT INTO acervo.ideia_evento (ideia_id, evento, de_status, para_status, motivo, autor)
  VALUES (NEW.id, v_evento, v_de, NEW.status, NEW.motivo_status, acervo.autor_atual());

  INSERT INTO acervo.outbox (agregado_id, tipo_evento, payload)
  VALUES (NEW.id, v_tipo,
          jsonb_build_object('de', v_de, 'para', NEW.status, 'origem', NEW.origem));
  RETURN NULL;
END $$;

CREATE TRIGGER ideia_registrar
  AFTER INSERT OR UPDATE OF status ON acervo.ideia
  FOR EACH ROW EXECUTE FUNCTION acervo.trg_ideia_registrar();

-- ---------------------------------------------------------------------
-- Permissões para FKs vindas de outros módulos (REFERENCES, sem SELECT)
-- ---------------------------------------------------------------------
GRANT REFERENCES ON acervo.ideia TO projetos_owner;

RESET ROLE;
