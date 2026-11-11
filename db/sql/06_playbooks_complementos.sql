-- =====================================================================
-- 06_playbooks_complementos.sql
-- Autor da proposta de versão (regra "quem propôs não promove") e
-- propostas de melhoria vindas das lições aprendidas (processo 2.2).
-- =====================================================================
SET ROLE playbooks_owner;

-- Versões já existentes recebem o autor corrente no momento do ALTER.
ALTER TABLE playbooks.playbook_versao
  ADD COLUMN proposto_por text NOT NULL DEFAULT playbooks.autor_atual();

-- Quem propôs não pode ser trocado depois (senão a regra de quatro olhos seria contornável).
CREATE FUNCTION playbooks.trg_proposto_por_imutavel() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.proposto_por IS DISTINCT FROM OLD.proposto_por THEN
    RAISE EXCEPTION 'proposto_por da versão % do playbook % não pode ser alterado',
      OLD.versao, OLD.playbook_id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER versao_proposto_por_imutavel
  BEFORE UPDATE ON playbooks.playbook_versao
  FOR EACH ROW EXECUTE FUNCTION playbooks.trg_proposto_por_imutavel();

-- ---------------------------------------------------------------------
-- Proposta de melhoria de playbook
-- origem_licao_id e origem_projeto_id são uuid lógicos (sem FK): o schema
-- playbooks nunca aponta para projetos.
-- ---------------------------------------------------------------------
CREATE TABLE playbooks.proposta_melhoria (
  id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  playbook_id        uuid        NOT NULL,
  versao_base        text        NOT NULL,
  origem_licao_id    uuid,                       -- lógico: projetos.licao_aprendida
  origem_projeto_id  uuid,                       -- lógico: projetos.projeto
  descricao          text        NOT NULL,
  status             text        NOT NULL DEFAULT 'pendente'
                     CHECK (status IN ('pendente', 'aceita', 'rejeitada')),
  versao_resultante  text,
  decidido_por       text,
  decidido_em        timestamptz,
  criado_em          timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (playbook_id, versao_base)
    REFERENCES playbooks.playbook_versao (playbook_id, versao) ON DELETE RESTRICT,
  FOREIGN KEY (playbook_id, versao_resultante)
    REFERENCES playbooks.playbook_versao (playbook_id, versao) ON DELETE RESTRICT,
  CHECK (status = 'pendente' OR (decidido_por IS NOT NULL AND decidido_em IS NOT NULL))
);

-- Idempotência: uma lição gera no máximo uma proposta.
CREATE UNIQUE INDEX proposta_melhoria_licao_uk
  ON playbooks.proposta_melhoria (origem_licao_id) WHERE origem_licao_id IS NOT NULL;

RESET ROLE;
