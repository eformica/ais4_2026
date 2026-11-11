-- =====================================================================
-- 07_acervo_similaridade.sql
-- Similaridade entre ideias (pgvector), lacunas de playbook e revisão de
-- duplicidade (critérios 3 e 4 da qualificação, seção 3.3).
-- A extensão é criada pelo superusuário ANTES de assumir acervo_owner; as
-- tabelas novas herdam os grants de acervo_app definidos em 05.
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS vector;

SET ROLE acervo_owner;

-- Um vetor por ideia. conteudo_sha256 permite não reindexar texto inalterado.
CREATE TABLE acervo.ideia_embedding (
  ideia_id         uuid        PRIMARY KEY REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  modelo           text        NOT NULL,
  conteudo_sha256  char(64)    NOT NULL,
  embedding        vector(1024) NOT NULL,
  gerado_em        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ideia_embedding_hnsw_idx
  ON acervo.ideia_embedding USING hnsw (embedding vector_cosine_ops);

-- Lacuna: a ideia é boa, mas nenhum playbook ativo a cobre (processo 1.2).
CREATE TABLE acervo.ideia_lacuna (
  id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  ideia_id            uuid        NOT NULL REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  descricao           text        NOT NULL,
  classes_requeridas  text[]      NOT NULL DEFAULT '{}',
  status              text        NOT NULL DEFAULT 'aberta'
                      CHECK (status IN ('aberta', 'reabrir', 'resolvida')),
  criado_em           timestamptz NOT NULL DEFAULT now(),
  resolvida_em        timestamptz
);
CREATE INDEX ideia_lacuna_ideia_idx ON acervo.ideia_lacuna (ideia_id);
CREATE INDEX ideia_lacuna_status_idx ON acervo.ideia_lacuna (status);

-- Revisão humana de um par (ideia, candidata a duplicata). Reavaliar o par
-- substitui a decisão anterior (revisor e data do último prevalecem).
CREATE TABLE acervo.ideia_revisao_duplicidade (
  ideia_id       uuid        NOT NULL REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  candidata_id   uuid        NOT NULL REFERENCES acervo.ideia (id) ON DELETE RESTRICT,
  similaridade   real        NOT NULL,
  decisao        text        NOT NULL CHECK (decisao IN ('distinta', 'duplicada')),
  revisor        text        NOT NULL,
  decidido_em    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ideia_id, candidata_id),
  CHECK (ideia_id <> candidata_id)
);

RESET ROLE;
