-- =====================================================================
-- 11_execucao_endurecimento.sql  —  R21 (Task 15)
-- 1. No máximo UMA tentativa ativa por etapa de projeto (despacho em
--    duplicidade vira violação de unicidade).
-- 2. Tarefa terminal (concluida, falhou, cancelada) é imutável: nenhum
--    campo muda, nem a reescrita para o mesmo status. Um worker zumbi cujo
--    lease já foi varrido não consegue sobrescrever o registro.
-- 3. O lease vale também durante a validação (o revisor é outra chamada
--    ao LLM e pode travar o worker); só é limpo fora de em_execucao/validando.
-- 4. Toda chamada ao LLM pertence a um projeto ou a uma proposta.
-- =====================================================================
SET ROLE execucao_owner;

CREATE UNIQUE INDEX tarefa_ativa_uq ON execucao.tarefa (projeto_id, etapa_codigo)
  WHERE status IN ('pronta', 'em_execucao', 'validando', 'aguardando_gate');

DROP INDEX execucao.tarefa_lease_idx;
CREATE INDEX tarefa_lease_idx ON execucao.tarefa (lease_ate)
  WHERE status IN ('em_execucao', 'validando');

CREATE OR REPLACE FUNCTION execucao.trg_tarefa_transicao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status IN ('concluida', 'falhou', 'cancelada') THEN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
      RAISE EXCEPTION 'Transição de status de tarefa inválida: % -> % (tarefa %): estado terminal',
        OLD.status, NEW.status, NEW.id
        USING ERRCODE = 'check_violation';
    END IF;
    RAISE EXCEPTION 'Tarefa % já terminou (%): estado terminal, todos os campos são imutáveis',
      OLD.id, OLD.status
      USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status || '>' || NEW.status) = ANY (ARRAY[
       'pronta>em_execucao',
       'em_execucao>validando',
       'em_execucao>falhou',
       'validando>concluida',
       'validando>falhou',
       'validando>aguardando_gate',
       'aguardando_gate>concluida',
       'aguardando_gate>falhou',
       'pronta>cancelada',
       'em_execucao>cancelada',
       'validando>cancelada',
       'aguardando_gate>cancelada'
     ]) THEN
    RAISE EXCEPTION 'Transição de status de tarefa inválida: % -> % (tarefa %)',
      OLD.status, NEW.status, NEW.id
      USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status IN ('concluida', 'falhou', 'cancelada') THEN
    NEW.finalizado_em := COALESCE(NEW.finalizado_em, now());
  END IF;
  IF NEW.status NOT IN ('em_execucao', 'validando') THEN
    NEW.lease_ate := NULL;
  END IF;
  RETURN NEW;
END $$;

ALTER TABLE execucao.chamada_llm
  ADD CONSTRAINT chamada_llm_dono_ck CHECK (projeto_id IS NOT NULL OR proposta_id IS NOT NULL);

RESET ROLE;
