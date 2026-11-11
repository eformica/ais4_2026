# Esquema mínimo — Sistema de Orquestração de Projetos de IA

PostgreSQL 13+. Um schema por módulo (`playbooks`, `acervo`, `projetos`), com FKs entre schemas mantidas na fase 1.

## Ordem de execução

```bash
for f in 00_roles_schemas.sql 01_playbooks.sql 02_acervo.sql 03_projetos.sql 04_seed.sql; do
  psql -v ON_ERROR_STOP=1 -d orquestracao -f "$f"
done
```

Executar como superusuário. Cada arquivo de módulo roda com `SET ROLE <modulo>_owner`, então as tabelas pertencem ao papel dono do módulo.

## Decisões refletidas no esquema

| Decisão | Como foi implementada |
| --- | --- |
| FKs entre schemas sem ciclo | `playbooks` ← `acervo` ← `projetos`, e `playbooks` ← `projetos`. O acervo não aponta para projetos: o vínculo da retroalimentação está em `projetos.proximo_passo.ideia_gerada_id`. |
| Fronteira de leitura preservada | Entre módulos só há `USAGE` no schema e `REFERENCES` nas tabelas de identidade; nenhum `SELECT` é concedido. |
| Nada é apagado em cascata | Todas as FKs usam `ON DELETE RESTRICT`; registros principais têm `excluido_em` (exclusão lógica). |
| Versões de playbook imutáveis | Depois de sair de `proposto`, conteúdo e etapas ficam bloqueados por trigger; apagar só é possível em `proposto`. |
| Especificação de projeto imutável | `projetos.especificacao` aceita apenas inserção; mudança de escopo = nova versão com justificativa. |
| Ciclos de vida explícitos | Triggers validam as transições de status de ideia, versão de playbook e projeto. |
| Projeto só fecha com retroalimentação | `concluido` exige lições, próximos passos todos convertidos em ideia ou descartados com motivo, e gates aprovados. |
| Histórico e eventos | Tabelas `*_evento` (somente inserção) e `outbox` por schema, preenchidas por trigger nas mudanças de status. |
| Autor das mudanças | A aplicação define `SET app.usuario = '...'` na sessão; sem isso, usa o papel do banco. |

## Fora deste esquema mínimo (próximos passos)

- Schema `execucao` (log de etapas, custo, tentativas), com tabela particionada por data.
- Papéis de login das aplicações (`*_app`) com `SELECT/INSERT/UPDATE` apenas no próprio schema.
- Checagem de duplicidade por similaridade (pgvector) no acervo.
- Ligação formal `projetos.marco.etapa_codigo` → `playbooks.etapa` (hoje é referência textual).
- Job de reconciliação e publicador do outbox.
- Regras que cruzam módulos sem FK (ex.: ideia precisa estar `qualificada` para virar projeto) ficam no Motor de Criação, que lê o acervo pela API do módulo.
