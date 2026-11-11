-- =====================================================================
-- 04_seed.sql  —  Sementes: playbook "Projeto Acadêmico Simples" v0.1.0
--                 e a ideia piloto (abastecimento alimentar 2020–2050)
-- =====================================================================

-- ---------------------------------------------------------------------
-- Playbook
-- ---------------------------------------------------------------------
SET ROLE playbooks_owner;

INSERT INTO playbooks.playbook (id, codigo, nome, descricao, classe_de_problema)
VALUES ('00000000-0000-4000-8000-000000000001',
        'projeto-academico-simples',
        'Projeto Acadêmico Simples',
        'Análise exploratória e descritiva com dados públicos, opcionalmente com projeções por cenário.',
        ARRAY['analise_exploratoria', 'analise_descritiva', 'projecao_por_cenarios']);

INSERT INTO playbooks.playbook_versao
  (playbook_id, versao, pre_requisitos, template_projeto, criterios_qualidade, entregaveis, changelog)
VALUES (
  '00000000-0000-4000-8000-000000000001', '0.1.0',
  '[{"tipo": "pergunta_norteadora", "descricao": "Pergunta respondível"},
    {"tipo": "dados", "descricao": "Ao menos uma fonte pública acessível"},
    {"tipo": "ambiente", "descricao": "Python com pandas, statsmodels, scikit-learn e geopandas"}]',
  '{"secoes": ["definicao_problema", "escopo", "objetivos", "restricoes", "metodologia",
               "fontes_dados", "resultados_esperados", "riscos", "cronograma"]}',
  '[{"codigo": "rastreabilidade", "descricao": "Toda afirmação numérica rastreável a uma fonte"},
    {"codigo": "observado_vs_projetado", "descricao": "Dados observados e projetados sempre distinguidos visualmente"},
    {"codigo": "incerteza", "descricao": "Projeções com intervalo de incerteza e premissas explícitas"},
    {"codigo": "reprodutibilidade", "descricao": "Código reexecutável do zero"},
    {"codigo": "limitacoes", "descricao": "Limitações declaradas"}]',
  '[{"tipo": "relatorio"}, {"tipo": "apresentacao"}, {"tipo": "dataset"}, {"tipo": "codigo"}]',
  'Versão inicial (semente).'
);

INSERT INTO playbooks.etapa
  (playbook_id, versao, codigo, ordem, nome, depende_de, papel_agente, saidas, criterio_saida, exige_gate, papel_revisor)
VALUES
  ('00000000-0000-4000-8000-000000000001', '0.1.0', 'contexto', 1,
   'Revisão de contexto e literatura', '{}',
   '{"papel": "pesquisador", "ferramentas": ["web_search", "web_fetch"]}',
   '["nota_de_contexto"]', 'Nota de contexto com referências verificáveis', false, NULL),
  ('00000000-0000-4000-8000-000000000001', '0.1.0', 'coleta_dados', 2,
   'Inventário e coleta de dados', '{contexto}',
   '{"papel": "engenheiro_de_dados", "ferramentas": ["web_fetch", "python"]}',
   '["catalogo_fontes", "dados_brutos"]', 'Fontes catalogadas com cobertura, licença e metadados', true, 'responsavel_por_dados'),
  ('00000000-0000-4000-8000-000000000001', '0.1.0', 'harmonizacao', 3,
   'Limpeza e harmonização', '{coleta_dados}',
   '{"papel": "engenheiro_de_dados", "ferramentas": ["python"]}',
   '["dataset_analitico", "dicionario_dados", "log_tratamento"]', 'Dataset analítico com dicionário e log', false, NULL),
  ('00000000-0000-4000-8000-000000000001', '0.1.0', 'analise_exploratoria', 4,
   'Análise exploratória', '{harmonizacao}',
   '{"papel": "analista", "ferramentas": ["python"]}',
   '["estatisticas_descritivas", "graficos"]', 'Estatísticas e gráficos cobrindo as variáveis do escopo', false, NULL),
  ('00000000-0000-4000-8000-000000000001', '0.1.0', 'modelagem', 5,
   'Modelagem e projeções', '{analise_exploratoria}',
   '{"papel": "modelador", "ferramentas": ["python"]}',
   '["modelos", "cenarios", "metricas_erro"]', 'Cenários com premissas, intervalos e métricas de erro', true, 'revisor_metodologico'),
  ('00000000-0000-4000-8000-000000000001', '0.1.0', 'sintese', 6,
   'Síntese e interpretação', '{modelagem}',
   '{"papel": "analista", "ferramentas": []}',
   '["achados", "limitacoes"]', 'Achados ligados a evidências; limitações declaradas', false, NULL),
  ('00000000-0000-4000-8000-000000000001', '0.1.0', 'relatorio', 7,
   'Relatório e apresentação', '{sintese}',
   '{"papel": "redator", "ferramentas": ["documentos", "slides"]}',
   '["relatorio", "apresentacao"]', 'Relatório e slides com insights, metodologia, resultados, discussão e próximos passos', true, 'dono_do_projeto'),
  ('00000000-0000-4000-8000-000000000001', '0.1.0', 'retroalimentacao', 8,
   'Retroalimentação', '{relatorio}',
   '{"papel": "curador", "ferramentas": []}',
   '["licoes_aprendidas", "novas_ideias"]', 'Lições registradas e próximos passos convertidos ou descartados', true, 'curador_do_acervo');

-- Sanidade do DAG antes de promover (deve retornar zero linhas)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM playbooks.dependencias_invalidas(
               '00000000-0000-4000-8000-000000000001', '0.1.0')) THEN
    RAISE EXCEPTION 'Playbook semente com dependências inválidas';
  END IF;
END $$;

UPDATE playbooks.playbook_versao
   SET status = 'experimental'
 WHERE playbook_id = '00000000-0000-4000-8000-000000000001'
   AND versao = '0.1.0';

RESET ROLE;

-- ---------------------------------------------------------------------
-- Ideia piloto
-- ---------------------------------------------------------------------
SET ROLE acervo_owner;

INSERT INTO acervo.ideia (
  id, titulo, tipo, descricao, pergunta_norteadora, dominio, origem,
  playbook_sugerido_id, fontes_candidatas, restricoes,
  valor, viabilidade, status, criado_por
) VALUES (
  '00000000-0000-4000-8000-000000000101',
  'Demografia, produção agropecuária e abastecimento no Brasil (2020–2050)',
  'pergunta_de_pesquisa',
  'O abastecimento com alimentos de qualidade depende do volume produzido, mas também de onde se produz, '
  'onde a população vive e da capacidade de transportar e armazenar. Mudanças demográficas e na produção '
  'até 2050 podem alterar demanda, rotas e disponibilidade por região.',
  'Como as mudanças demográficas e a evolução projetada da produção agropecuária entre 2020 e 2050 podem '
  'afetar a disponibilidade e a distribuição regional de alimentos nos centros consumidores?',
  ARRAY['agro', 'demografia', 'logistica', 'seguranca_alimentar'],
  'humana',
  '00000000-0000-4000-8000-000000000001',
  '[{"nome": "IBGE - Censo 2022"},
    {"nome": "IBGE - Projeções da População"},
    {"nome": "IBGE - Produção Agrícola Municipal (PAM)"},
    {"nome": "IBGE - Pesquisa da Pecuária Municipal (PPM)"},
    {"nome": "IBGE - Pesquisa de Orçamentos Familiares (POF)"},
    {"nome": "IBGE - Censo Agropecuário"},
    {"nome": "Conab - safras e armazenagem"},
    {"nome": "MAPA - Projeções do Agronegócio"},
    {"nome": "Comex Stat"},
    {"nome": "Ministério dos Transportes / ONTL"}]',
  'Horizonte 2050; projeções tratadas como cenários; consumo regional estimado por proxy.',
  4, 3,            -- valores provisórios, a calibrar na triagem
  'triagem',
  'semente'
);

RESET ROLE;
