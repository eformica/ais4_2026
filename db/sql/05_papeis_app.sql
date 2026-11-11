-- =====================================================================
-- 05_papeis_app.sql
-- Papéis de login das aplicações: um por módulo, com acesso só ao próprio
-- schema. A senha NÃO é definida aqui: a migração Python faz
-- ALTER ROLE ... PASSWORD a partir da configuração.
-- Sem DELETE em lugar nenhum: a exclusão é lógica (excluido_em).
-- Os grants de criacao/execucao ficam nos arquivos 09 e 10, que usam
-- ALTER DEFAULT PRIVILEGES para as tabelas que eles mesmos criam.
-- =====================================================================

DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['playbooks_app', 'acervo_app', 'projetos_app',
                           'criacao_app', 'execucao_app'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I LOGIN', r);
    END IF;
  END LOOP;
END $$;

DO $$
DECLARE
  m text;
BEGIN
  FOREACH m IN ARRAY ARRAY['playbooks', 'acervo', 'projetos'] LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', m, m || '_app');
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA %I TO %I',
                   m, m || '_app');
    EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I',
                   m, m || '_app');
    -- Objetos criados depois pelo dono do schema herdam os mesmos grants.
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                   'GRANT SELECT, INSERT, UPDATE ON TABLES TO %I',
                   m || '_owner', m, m || '_app');
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                   'GRANT USAGE, SELECT ON SEQUENCES TO %I',
                   m || '_owner', m, m || '_app');
  END LOOP;
END $$;
