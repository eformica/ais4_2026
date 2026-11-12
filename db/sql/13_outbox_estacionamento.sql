-- =====================================================================
-- 13_outbox_estacionamento.sql  —  R32 (revisão final #4)
-- Um evento cujo handler falha sempre não pode travar o outbox do módulo.
-- Cada falha incrementa `tentativas` e guarda `ultimo_erro`; ao atingir o
-- limite (AIS4_MAX_TENTATIVAS_EVENTO) o publicador grava `estacionado_em`
-- e segue com os eventos seguintes. Evento estacionado sai da fila de
-- pendentes até alguém reprocessá-lo à mão (ver docs/piloto.md, seção 5).
-- =====================================================================

SET ROLE playbooks_owner;
ALTER TABLE playbooks.outbox
  ADD COLUMN tentativas     int         NOT NULL DEFAULT 0 CHECK (tentativas >= 0),
  ADD COLUMN ultimo_erro    text,
  ADD COLUMN estacionado_em timestamptz;
DROP INDEX playbooks.outbox_pendentes_idx;
CREATE INDEX outbox_pendentes_idx ON playbooks.outbox (criado_em)
  WHERE publicado_em IS NULL AND estacionado_em IS NULL;
CREATE INDEX outbox_estacionados_idx ON playbooks.outbox (estacionado_em)
  WHERE estacionado_em IS NOT NULL;
RESET ROLE;

SET ROLE acervo_owner;
ALTER TABLE acervo.outbox
  ADD COLUMN tentativas     int         NOT NULL DEFAULT 0 CHECK (tentativas >= 0),
  ADD COLUMN ultimo_erro    text,
  ADD COLUMN estacionado_em timestamptz;
DROP INDEX acervo.outbox_pendentes_idx;
CREATE INDEX outbox_pendentes_idx ON acervo.outbox (criado_em)
  WHERE publicado_em IS NULL AND estacionado_em IS NULL;
CREATE INDEX outbox_estacionados_idx ON acervo.outbox (estacionado_em)
  WHERE estacionado_em IS NOT NULL;
RESET ROLE;

SET ROLE projetos_owner;
ALTER TABLE projetos.outbox
  ADD COLUMN tentativas     int         NOT NULL DEFAULT 0 CHECK (tentativas >= 0),
  ADD COLUMN ultimo_erro    text,
  ADD COLUMN estacionado_em timestamptz;
DROP INDEX projetos.outbox_pendentes_idx;
CREATE INDEX outbox_pendentes_idx ON projetos.outbox (criado_em)
  WHERE publicado_em IS NULL AND estacionado_em IS NULL;
CREATE INDEX outbox_estacionados_idx ON projetos.outbox (estacionado_em)
  WHERE estacionado_em IS NOT NULL;
RESET ROLE;

SET ROLE criacao_owner;
ALTER TABLE criacao.outbox
  ADD COLUMN tentativas     int         NOT NULL DEFAULT 0 CHECK (tentativas >= 0),
  ADD COLUMN ultimo_erro    text,
  ADD COLUMN estacionado_em timestamptz;
DROP INDEX criacao.outbox_pendentes_idx;
CREATE INDEX outbox_pendentes_idx ON criacao.outbox (criado_em)
  WHERE publicado_em IS NULL AND estacionado_em IS NULL;
CREATE INDEX outbox_estacionados_idx ON criacao.outbox (estacionado_em)
  WHERE estacionado_em IS NOT NULL;
RESET ROLE;

SET ROLE execucao_owner;
ALTER TABLE execucao.outbox
  ADD COLUMN tentativas     int         NOT NULL DEFAULT 0 CHECK (tentativas >= 0),
  ADD COLUMN ultimo_erro    text,
  ADD COLUMN estacionado_em timestamptz;
DROP INDEX execucao.outbox_pendentes_idx;
CREATE INDEX outbox_pendentes_idx ON execucao.outbox (criado_em)
  WHERE publicado_em IS NULL AND estacionado_em IS NULL;
CREATE INDEX outbox_estacionados_idx ON execucao.outbox (estacionado_em)
  WHERE estacionado_em IS NOT NULL;
RESET ROLE;
