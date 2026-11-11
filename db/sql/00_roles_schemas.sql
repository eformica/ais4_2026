-- =====================================================================
-- 00_roles_schemas.sql
-- Um schema por módulo, cada um com um papel dono (NOLOGIN).
-- Executar como superusuário (ou papel com CREATEROLE + CREATE na base).
-- Requer PostgreSQL 13+ (gen_random_uuid() nativo).
-- =====================================================================

DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['playbooks_owner', 'acervo_owner', 'projetos_owner'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END $$;

CREATE SCHEMA IF NOT EXISTS playbooks AUTHORIZATION playbooks_owner;
CREATE SCHEMA IF NOT EXISTS acervo    AUTHORIZATION acervo_owner;
CREATE SCHEMA IF NOT EXISTS projetos  AUTHORIZATION projetos_owner;

REVOKE ALL ON SCHEMA playbooks, acervo, projetos FROM PUBLIC;

-- USAGE no schema é necessário para declarar FKs que apontam para ele.
-- Não concede leitura de tabelas: isso exige SELECT, que não é dado.
-- Grafo de dependências (sem ciclos):
--   playbooks  <-  acervo  <-  projetos
--   playbooks  <-------------  projetos
GRANT USAGE ON SCHEMA playbooks TO acervo_owner, projetos_owner;
GRANT USAGE ON SCHEMA acervo    TO projetos_owner;
