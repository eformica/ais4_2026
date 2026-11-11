-- =====================================================================
-- 08_projetos_complementos.sql
-- Responsáveis do projeto (seção 5.1) e trilha dos gates: transição de
-- status do marco validada no banco, com histórico e outbox.
-- As tabelas novas herdam os grants de projetos_app definidos em 05.
-- =====================================================================
SET ROLE projetos_owner;

-- Quem responde por cada papel no projeto (dono, revisores dos gates).
CREATE TABLE projetos.responsavel (
  projeto_id  uuid  NOT NULL REFERENCES projetos.projeto (id) ON DELETE RESTRICT,
  papel       text  NOT NULL,
  pessoa      text  NOT NULL,
  PRIMARY KEY (projeto_id, papel)
);

-- ---------------------------------------------------------------------
-- Ciclo de vida do marco
-- ---------------------------------------------------------------------
CREATE FUNCTION projetos.trg_marco_transicao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;

  IF NOT (OLD.status || '>' || NEW.status) = ANY (ARRAY[
       'pendente>em_andamento',
       'em_andamento>aguardando_gate',
       'aguardando_gate>aprovado',
       'aguardando_gate>reprovado',
       'reprovado>em_andamento',
       'em_andamento>aprovado',
       'pendente>cancelado',
       'em_andamento>cancelado',
       'aguardando_gate>cancelado',
       'reprovado>cancelado'
     ]) THEN
    RAISE EXCEPTION 'Transição de status de marco inválida: % -> % (marco %)',
      OLD.status, NEW.status, NEW.codigo
      USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status = 'aguardando_gate' AND NOT NEW.exige_gate THEN
    RAISE EXCEPTION 'Transição de status de marco inválida: marco % não exige gate', NEW.codigo
      USING ERRCODE = 'check_violation';
  END IF;

  IF OLD.status = 'em_andamento' AND NEW.status = 'aprovado' AND NEW.exige_gate THEN
    RAISE EXCEPTION 'Transição de status de marco inválida: marco % exige gate e só é aprovado a partir de aguardando_gate',
      NEW.codigo
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER marco_transicao
  BEFORE UPDATE OF status ON projetos.marco
  FOR EACH ROW EXECUTE FUNCTION projetos.trg_marco_transicao();

-- Histórico e evento de integração (reprovado reabre a etapa na execução).
CREATE FUNCTION projetos.trg_marco_registrar() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NULL;
  END IF;

  INSERT INTO projetos.projeto_evento (projeto_id, evento, de_status, para_status, motivo, autor)
  VALUES (NEW.projeto_id, 'marco_' || NEW.status, OLD.status, NEW.status, NEW.codigo,
          projetos.autor_atual());

  INSERT INTO projetos.outbox (agregado_id, tipo_evento, payload)
  VALUES (NEW.projeto_id, 'MarcoStatusAlterado',
          jsonb_build_object('codigo', NEW.codigo, 'etapa_codigo', NEW.etapa_codigo,
                             'de', OLD.status, 'para', NEW.status,
                             'decisao_gate', NEW.decisao_gate));
  RETURN NULL;
END $$;

CREATE TRIGGER marco_registrar
  AFTER UPDATE OF status ON projetos.marco
  FOR EACH ROW EXECUTE FUNCTION projetos.trg_marco_registrar();

RESET ROLE;
