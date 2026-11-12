-- =====================================================================
-- 12_chamada_llm_ideia.sql  —  R30 (revisão final #3)
-- O enriquecimento 1.1 (processo do Acervo, sob demanda) chama o LLM sem
-- projeto nem proposta. Para que esse gasto fique registrado, a chamada
-- pode pertencer a uma ideia.
-- `ideia_id` é referência LÓGICA a acervo.ideia (sem FK): execucao não
-- depende de acervo no grafo A.3 e esta coluna não cria essa aresta.
-- =====================================================================
SET ROLE execucao_owner;

ALTER TABLE execucao.chamada_llm ADD COLUMN ideia_id uuid;
COMMENT ON COLUMN execucao.chamada_llm.ideia_id IS
  'Referência lógica (sem FK) a acervo.ideia: chamadas do enriquecimento 1.1';

ALTER TABLE execucao.chamada_llm DROP CONSTRAINT chamada_llm_dono_ck;
ALTER TABLE execucao.chamada_llm
  ADD CONSTRAINT chamada_llm_dono_ck
  CHECK (projeto_id IS NOT NULL OR proposta_id IS NOT NULL OR ideia_id IS NOT NULL);

CREATE INDEX chamada_llm_ideia_idx ON execucao.chamada_llm (ideia_id) WHERE ideia_id IS NOT NULL;

RESET ROLE;
