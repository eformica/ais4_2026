-- =====================================================================
-- 03_projetos.sql  —  Base de Projetos (seção 5)
-- Depende de: acervo (ideia de origem, ideias derivadas)
--             playbooks (playbook + versão exata, playbook alvo de lições)
-- =====================================================================
SET ROLE projetos_owner;

-- ---------------------------------------------------------------------
-- Utilitários do schema
-- ---------------------------------------------------------------------
CREATE FUNCTION projetos.autor_atual() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(NULLIF(current_setting('app.usuario', true), ''), current_user::text)
$$;

CREATE FUNCTION projetos.trg_atualizado_em() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.atualizado_em := now();
  RETURN NEW;
END $$;

CREATE FUNCTION projetos.trg_somente_insercao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Tabela %.% aceita apenas inserções', TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'check_violation';
END $$;

-- ---------------------------------------------------------------------
-- Projeto
-- ---------------------------------------------------------------------
CREATE TABLE projetos.projeto (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  titulo           text        NOT NULL,
  ideia_origem_id  uuid        NOT NULL REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  playbook_id      uuid        NOT NULL,
  playbook_versao  text        NOT NULL,
  status           text        NOT NULL DEFAULT 'aprovado'
                   CHECK (status IN ('aprovado', 'em_execucao', 'em_revisao',
                                     'concluido', 'pausado', 'cancelado')),
  motivo_status    text,
  dono             text        NOT NULL,
  criado_em        timestamptz NOT NULL DEFAULT now(),
  atualizado_em    timestamptz NOT NULL DEFAULT now(),
  excluido_em      timestamptz,
  FOREIGN KEY (playbook_id, playbook_versao)
    REFERENCES playbooks.playbook_versao (playbook_id, versao) ON DELETE RESTRICT,
  CHECK (status NOT IN ('pausado', 'cancelado') OR motivo_status IS NOT NULL)
);

CREATE INDEX projeto_status_idx ON projetos.projeto (status) WHERE excluido_em IS NULL;
CREATE INDEX projeto_ideia_idx  ON projetos.projeto (ideia_origem_id);

-- ---------------------------------------------------------------------
-- Especificação versionada (estrutura mínima 4.1) — imutável
-- ---------------------------------------------------------------------
CREATE TABLE projetos.especificacao (
  projeto_id     uuid        NOT NULL REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  versao         integer     NOT NULL CHECK (versao > 0),
  conteudo       jsonb       NOT NULL,
  justificativa  text,
  aprovado_por   text        NOT NULL,
  aprovado_em    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (projeto_id, versao),
  CHECK (jsonb_typeof(conteudo) = 'object'
         AND conteudo ?& ARRAY['definicao_problema', 'escopo', 'objetivos', 'restricoes',
                               'metodologia', 'fontes_dados', 'resultados_esperados',
                               'riscos', 'cronograma']),
  CHECK (versao = 1 OR justificativa IS NOT NULL)   -- mudança de escopo exige justificativa
);

CREATE TRIGGER especificacao_somente_insercao
  BEFORE UPDATE OR DELETE ON projetos.especificacao
  FOR EACH ROW EXECUTE FUNCTION projetos.trg_somente_insercao();

-- ---------------------------------------------------------------------
-- Marcos e gates
-- ---------------------------------------------------------------------
CREATE TABLE projetos.marco (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  projeto_id       uuid        NOT NULL REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  codigo           text        NOT NULL,              -- ex.: 'M1'
  ordem            smallint    NOT NULL CHECK (ordem > 0),
  nome             text        NOT NULL,
  etapa_codigo     text,                              -- etapa do playbook correspondente
  criterio_saida   text        NOT NULL,
  exige_gate       boolean     NOT NULL DEFAULT false,
  papel_revisor    text,                              -- ex.: 'dono_do_projeto'
  revisor          text,                              -- pessoa que decidiu o gate
  status           text        NOT NULL DEFAULT 'pendente'
                   CHECK (status IN ('pendente', 'em_andamento', 'aguardando_gate',
                                     'aprovado', 'reprovado', 'cancelado')),
  data_planejada   date,
  data_real        date,
  decisao_gate     text,
  decidido_em      timestamptz,
  UNIQUE (projeto_id, codigo),
  UNIQUE (projeto_id, ordem),
  CHECK (NOT exige_gate OR papel_revisor IS NOT NULL),
  CHECK (status NOT IN ('aprovado', 'reprovado') OR NOT exige_gate
         OR (revisor IS NOT NULL AND decidido_em IS NOT NULL))
);

-- ---------------------------------------------------------------------
-- Artefatos (arquivos ficam em armazenamento de objetos; aqui só metadados)
-- ---------------------------------------------------------------------
CREATE TABLE projetos.artefato (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  projeto_id    uuid        NOT NULL REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  marco_id      uuid        REFERENCES projetos.marco (id) ON DELETE RESTRICT,
  tipo          text        NOT NULL
                CHECK (tipo IN ('dataset', 'codigo', 'grafico', 'relatorio', 'apresentacao', 'outro')),
  nome          text        NOT NULL,
  uri           text        NOT NULL,
  hash_sha256   char(64)    NOT NULL CHECK (hash_sha256 ~ '^[0-9a-f]{64}$'),
  proveniencia  jsonb       NOT NULL DEFAULT '{}',    -- fontes, etapa, agente, versão do código
  criado_em     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artefato_projeto_idx ON projetos.artefato (projeto_id);

-- ---------------------------------------------------------------------
-- Avaliação por critério de qualidade do playbook
-- ---------------------------------------------------------------------
CREATE TABLE projetos.avaliacao (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  projeto_id   uuid        NOT NULL REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  criterio     text        NOT NULL,
  nota         smallint    NOT NULL CHECK (nota BETWEEN 1 AND 5),
  comentario   text,
  revisor      text        NOT NULL,
  avaliado_em  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Lições aprendidas (alimentam a Base de Playbooks)
-- ---------------------------------------------------------------------
CREATE TABLE projetos.licao_aprendida (
  id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  projeto_id        uuid        NOT NULL REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  tipo              text        NOT NULL CHECK (tipo IN ('funcionou', 'falhou', 'sugestao')),
  descricao         text        NOT NULL,
  playbook_alvo_id  uuid        REFERENCES playbooks.playbook (id) ON DELETE RESTRICT,
  encaminhada_em    timestamptz,
  criado_em         timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Próximos passos -> ideias derivadas (processo 2.2)
-- Este é o vínculo projeto -> ideia gerada; o acervo não aponta de volta.
-- ---------------------------------------------------------------------
CREATE TABLE projetos.proximo_passo (
  id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  projeto_id         uuid        NOT NULL REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  descricao          text        NOT NULL,
  ideia_gerada_id    uuid        UNIQUE REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  descartado_motivo  text,
  criado_em          timestamptz NOT NULL DEFAULT now(),
  CHECK (ideia_gerada_id IS NULL OR descartado_motivo IS NULL)
);
CREATE INDEX proximo_passo_projeto_idx ON projetos.proximo_passo (projeto_id);

-- ---------------------------------------------------------------------
-- Histórico (somente inserção) e outbox
-- ---------------------------------------------------------------------
CREATE TABLE projetos.projeto_evento (
  id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  projeto_id   uuid        NOT NULL REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  evento       text        NOT NULL,
  de_status    text,
  para_status  text,
  motivo       text,
  autor        text        NOT NULL,
  ocorrido_em  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX projeto_evento_projeto_idx ON projetos.projeto_evento (projeto_id, ocorrido_em);

CREATE TRIGGER projeto_evento_somente_insercao
  BEFORE UPDATE OR DELETE ON projetos.projeto_evento
  FOR EACH ROW EXECUTE FUNCTION projetos.trg_somente_insercao();

CREATE TABLE projetos.outbox (
  id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agregado_id   uuid        NOT NULL,
  tipo_evento   text        NOT NULL,
  payload       jsonb       NOT NULL DEFAULT '{}',
  criado_em     timestamptz NOT NULL DEFAULT now(),
  publicado_em  timestamptz
);
CREATE INDEX outbox_pendentes_idx ON projetos.outbox (criado_em) WHERE publicado_em IS NULL;

-- ---------------------------------------------------------------------
-- Regras de ciclo de vida do projeto
-- ---------------------------------------------------------------------
CREATE FUNCTION projetos.trg_projeto_transicao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;

  IF NOT (OLD.status || '>' || NEW.status) = ANY (ARRAY[
       'aprovado>em_execucao',
       'aprovado>cancelado',
       'em_execucao>em_revisao',
       'em_execucao>pausado',
       'em_execucao>cancelado',
       'pausado>em_execucao',
       'pausado>cancelado',
       'em_revisao>em_execucao',   -- revisão pede ajustes
       'em_revisao>concluido'
     ]) THEN
    RAISE EXCEPTION 'Transição de status de projeto inválida: % -> %', OLD.status, NEW.status
      USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status = 'em_execucao' AND OLD.status = 'aprovado'
     AND NOT EXISTS (SELECT 1 FROM projetos.especificacao WHERE projeto_id = NEW.id) THEN
    RAISE EXCEPTION 'Projeto % sem especificação aprovada não pode iniciar execução', NEW.id
      USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status = 'concluido' THEN
    IF NOT EXISTS (SELECT 1 FROM projetos.licao_aprendida WHERE projeto_id = NEW.id) THEN
      RAISE EXCEPTION 'Projeto % não pode ser concluído sem lições aprendidas', NEW.id
        USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM projetos.proximo_passo WHERE projeto_id = NEW.id) THEN
      RAISE EXCEPTION 'Projeto % não pode ser concluído sem próximos passos', NEW.id
        USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM projetos.proximo_passo
                WHERE projeto_id = NEW.id
                  AND ideia_gerada_id IS NULL
                  AND descartado_motivo IS NULL) THEN
      RAISE EXCEPTION 'Projeto % tem próximos passos sem retroalimentação (ideia gerada ou descarte)', NEW.id
        USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM projetos.marco
                WHERE projeto_id = NEW.id
                  AND exige_gate
                  AND status NOT IN ('aprovado', 'cancelado')) THEN
      RAISE EXCEPTION 'Projeto % tem gates não aprovados', NEW.id
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER projeto_transicao
  BEFORE UPDATE OF status ON projetos.projeto
  FOR EACH ROW EXECUTE FUNCTION projetos.trg_projeto_transicao();

CREATE TRIGGER projeto_atualizado_em
  BEFORE UPDATE ON projetos.projeto
  FOR EACH ROW EXECUTE FUNCTION projetos.trg_atualizado_em();

CREATE FUNCTION projetos.trg_projeto_registrar() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_de     text;
  v_evento text := 'criado';
  v_tipo   text := 'ProjetoCriado';
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
      RETURN NULL;
    END IF;
    v_de     := OLD.status;
    v_evento := 'status_alterado';
    v_tipo   := 'ProjetoStatusAlterado';
  END IF;

  INSERT INTO projetos.projeto_evento (projeto_id, evento, de_status, para_status, motivo, autor)
  VALUES (NEW.id, v_evento, v_de, NEW.status, NEW.motivo_status, projetos.autor_atual());

  -- 'em_revisao' dispara o processo 2.2; 'concluido' e 'cancelado' liberam a ideia de origem
  INSERT INTO projetos.outbox (agregado_id, tipo_evento, payload)
  VALUES (NEW.id, v_tipo,
          jsonb_build_object('de', v_de, 'para', NEW.status,
                             'ideia_origem_id', NEW.ideia_origem_id,
                             'playbook_id', NEW.playbook_id,
                             'playbook_versao', NEW.playbook_versao));
  RETURN NULL;
END $$;

CREATE TRIGGER projeto_registrar
  AFTER INSERT OR UPDATE OF status ON projetos.projeto
  FOR EACH ROW EXECUTE FUNCTION projetos.trg_projeto_registrar();

RESET ROLE;
