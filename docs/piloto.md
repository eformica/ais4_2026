# Piloto: roteiro de operação

Este roteiro é para quem vai operar o piloto: as pessoas que ocupam os papéis da seção 10 da
especificação (`../docs/especificacao-sistema-orquestracao-ia.md`) e quem cuida da infraestrutura.
Ele leva a ideia semente ("Demografia, produção agropecuária e abastecimento no Brasil,
2020–2050") da triagem até um projeto concluído e às novas ideias que ele gera.

O comportamento descrito aqui é o que o teste `tests/test_ciclo_completo.py` executa com o LLM, os
embeddings e o sandbox substituídos por falsos. Se o roteiro e o teste divergirem, o teste vale.

## 1. Pré-requisitos

| O quê | Para quê |
| --- | --- |
| Docker Desktop em execução | Banco PostgreSQL (container) e sandbox dos agentes |
| `uv` | Instala o Python 3.12 e as dependências (`uv sync`) |
| Python 3.12 | Instalado pelo `uv` a partir do `.python-version` |
| `ANTHROPIC_API_KEY` | Chamadas ao Claude (Motor de Criação, execução, revisor) |
| Chave da Voyage AI | Embeddings da similaridade de ideias (`AIS4_VOYAGE_API_KEY`) |

Todos os comandos abaixo rodam no diretório `src/` do repositório e assumem um shell POSIX (no
Windows, Git Bash ou WSL): `export`, `cp`, `$VAR` e as quebras de linha com `\` dos `curl`. Cada processo de longa duração
(API e workers) ocupa um terminal próprio.

```bash
uv sync
docker compose up -d db          # PostgreSQL 17 + pgvector, porta 5432
```

## 2. Configuração

### 2.1 `.env`

```bash
cp .env.exemplo .env
```

Edite o `.env`:

- `AIS4_SENHA_APP`: troque os `"trocar"` por senhas de verdade, uma por módulo. A migração cria os
  papéis `*_app` do banco com essas senhas na primeira execução: defina-as antes. Trocar depois exige
  `ALTER ROLE <modulo>_app PASSWORD '...'` no banco e o mesmo valor no `.env`.
- `AIS4_VOYAGE_API_KEY`: chave da Voyage AI.
- `AIS4_EMBEDDING_MODELO`: o padrão (`voyage-multilingual-2`) é um valor provisório; confirme na
  documentação da Voyage o nome do modelo multilíngue de 1024 dimensões e ajuste se for outro.
- Os demais valores são os provisórios do piloto (seção 6).

A chave da Anthropic **não** é lida do `.env`: o SDK a lê do ambiente do processo. Exporte-a em
cada terminal que sobe API, `motor-criacao` ou `worker-execucao`:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### 2.2 `config/usuarios.toml`: uma pessoa por papel

```bash
cp config/usuarios.exemplo.toml config/usuarios.toml     # o arquivo é ignorado pelo git
```

No piloto, atribua **uma pessoa a cada papel** (a spec permite acumular papéis, mas várias regras
de quatro olhos ficam sem efeito prático se a mesma pessoa acumula). Exemplo com as sete pessoas:

```toml
[[usuario]]
nome = "carla"
token_sha256 = "<hash>"
papeis = ["curador_do_acervo"]

[[usuario]]
nome = "ana"
token_sha256 = "<hash>"
papeis = ["dono_do_projeto"]

[[usuario]]
nome = "edu"
token_sha256 = "<hash>"
papeis = ["especialista_de_dominio"]

[[usuario]]
nome = "rita"
token_sha256 = "<hash>"
papeis = ["revisor_metodologico"]

[[usuario]]
nome = "davi"
token_sha256 = "<hash>"
papeis = ["responsavel_por_dados"]

[[usuario]]
nome = "fred"
token_sha256 = "<hash>"
papeis = ["guardiao_de_playbooks"]

[[usuario]]
nome = "tati"
token_sha256 = "<hash>"
papeis = ["responsavel_tecnico"]
```

Gere um token por pessoa, entregue o token a ela e guarde só o hash no arquivo:

```bash
uv run python -c "import secrets,hashlib; t=secrets.token_urlsafe(32); print('token:', t); print('hash: ', hashlib.sha256(t.encode()).hexdigest())"
```

A pessoa envia o token em todo pedido: `Authorization: Bearer <token>`. A API também tem a
interface Swagger em `http://127.0.0.1:8000/docs` (botão "Authorize").

A API lê o `config/usuarios.toml` uma vez e o guarda em cache: **incluir ou trocar uma pessoa
exige reiniciar a API**. Dois usuários com o mesmo `token_sha256` (ou um papel desconhecido)
fazem a carga falhar com a mensagem do problema; sem o arquivo, todo pedido autenticado responde
500 com `{"erro": "arquivo de usuários não encontrado: <caminho>"}`.

Regras de quatro olhos que valem no piloto, e por que as pessoas acima precisam ser distintas:

- quem pediu o ciclo de criação (`tati`, ou o worker `motor-criacao`) não decide a proposta;
- a curadora (`carla`) não pode qualificar a ideia que ela mesma escreveu (a semente foi escrita
  por `semente`, então qualquer curador pode);
- quem propôs uma versão de playbook não a promove;
- cada gate é decidido **só** pela pessoa designada na aprovação do projeto (seção 4.3).

### 2.3 Imagem do sandbox

Os agentes rodam código Python num container (`ais4-sandbox:0.1.0`: pandas, statsmodels,
scikit-learn, geopandas com versões fixadas). Construa a imagem uma vez:

```bash
docker build -t ais4-sandbox:0.1.0 sandbox/
```

### 2.4 Migração

```bash
uv run python -m ais4 migrar
```

Cria os schemas, os papéis do banco e a semente: o playbook "Projeto Acadêmico Simples" 0.1.0 (em
status `experimental`) e a ideia piloto, em `triagem`.

## 3. Processos

| Terminal | Comando | Instâncias |
| --- | --- | --- |
| 1 | `uv run python -m ais4 api` (porta 8000; `--host`, `--porta`) | 1 |
| 2 | `uv run python -m ais4 worker-outbox` | **exatamente 1 por banco** |
| 3 | `uv run python -m ais4 motor-criacao` (`--intervalo` em segundos, padrão 30) | 1 (opcional; ver abaixo) |
| 4 | `uv run python -m ais4 worker-execucao` (`--intervalo`, padrão 5) | 1 |

- **Na subida**, `api`, `worker-outbox`, `motor-criacao` e `worker-execucao` sincronizam as
  classes de playbook com o banco: uma versão nova entra como `proposto`; uma versão já gravada que
  difere da classe impede a subida (**código de saída 3**, com os campos divergentes no log). Uma
  versão publicada não muda: desfaça a edição ou publique-a como nova versão (semver).
- **`worker-outbox`: exatamente uma instância por banco (R15).** Ele entrega os eventos entre
  módulos: `ProjetoCriado` leva a ideia a `em_projeto`; `em_revisao` dispara o processo 2.2;
  `concluido` leva a ideia a `concluida`; gate reprovado reabre a etapa; pausa cancela tarefas. Com
  duas, a ordem de entrega dos eventos deixa de ser garantida; o piloto assume uma só. Sem ele,
  nada anda entre os módulos. Um evento cujo handler falha é tentado de novo a cada volta (os
  seguintes do mesmo módulo esperam, para manter a ordem); depois de 10 falhas
  (`AIS4_MAX_TENTATIVAS_EVENTO`) ele é **estacionado**, o log registra ERROR e os seguintes andam
  (seção 5).
- **`motor-criacao`** faz o fluxo 4.2 em loop sobre a ideia `qualificada` de maior prioridade. Você
  pode dispará-lo sob demanda pela API (`POST /criacao/ciclos`, seção 4.2) e deixar o worker
  desligado. Com um projeto `em_execucao`, o motor não cria proposta nova (limite
  `AIS4_MAX_PROJETOS_EM_EXECUCAO=1`). Limites contra gasto sem fim:
  - cada proposta tem um teto de custo de 5 USD (`AIS4_ORCAMENTO_PROPOSTA_USD`); ao atingi-lo, a
    proposta é `devolvida` com o motivo "orçamento da proposta excedido: ...";
  - depois de 3 propostas seguidas `devolvidas` para a mesma ideia (`AIS4_MAX_DEVOLUCOES_IDEIA`),
    a ideia volta a `triagem` com o motivo "3 criações seguidas falharam: <último motivo>" para a
    curadora corrigir e requalificar (a contagem recomeça na requalificação);
  - erro transitório da API (429, 5xx, 529 sobrecarga, conexão) devolve a proposta com o motivo
    `api_transitoria: ...`, não conta como falha da ideia e não marca nenhuma fonte como
    inacessível; o laço dobra a espera a cada ciclo assim (até 30 min) e volta ao `--intervalo`
    no primeiro ciclo normal;
  - proposta em `rascunho`/`em_autocritica` sem atividade há 120 min
    (`AIS4_PROPOSTA_ABANDONADA_MIN`; um motor morto por SIGKILL/OOM) é devolvida no início do
    próximo ciclo, com o motivo "proposta abandonada (sem atividade há N min)", e libera a ideia.
- **`worker-execucao`** executa as etapas do DAG do projeto em `em_execucao`. Ctrl+C termina a
  tentativa em andamento; um segundo Ctrl+C interrompe de vez e marca a tentativa como falha
  (`interrompido`).

Verifique: `curl http://127.0.0.1:8000/saude` devolve `{"status":"ok"}`.

## 4. Roteiro do piloto, gate a gate

Nos exemplos, `$T_CARLA`, `$T_ANA` etc. são os tokens de cada pessoa e `$API` é
`http://127.0.0.1:8000`. O corpo das respostas é JSON; erros vêm como `{"erro": "..."}` com os
códigos 401 (sem token), 403 (papel ou pessoa errada), 404, 409 (regra violada) e 422 (corpo
inválido).

```bash
API=http://127.0.0.1:8000
IDEIA=00000000-0000-4000-8000-000000000101      # a semente
```

### 4.1 Qualificação da ideia (curadora)

Antes de qualificar, fixe o critério operacional de "alimentos de qualidade" (seção 6): é um ponto
em aberto que precisa estar na especificação antes da execução. A curadora pode registrar o
critério na ideia:

```bash
curl -X PATCH $API/acervo/ideias/$IDEIA -H "Authorization: Bearer $T_CARLA" \
  -H "Content-Type: application/json" \
  -d '{"restricoes": "Horizonte 2050; projeções tratadas como cenários; consumo regional estimado por proxy. Alimentos de qualidade = in natura e minimamente processados (Guia Alimentar)."}'
```

O `PATCH` **substitui o campo inteiro**: o exemplo mantém o texto que a semente já tem em
`restricoes` e acrescenta o critério. Se o texto atual for outro, confira com
`curl $API/acervo/ideias/$IDEIA -H "Authorization: Bearer $T_CARLA"` e preserve-o.

Quem edita: o autor da ideia enquanto ela está em `rascunho` ou `triagem`; depois disso, só a
curadora (403 para os demais). Em `em_projeto`, `concluida`, `fundida` ou `arquivada` ninguém
edita (409). Cada edição fica no histórico (`GET /acervo/ideias/{id}/historico`) como evento
`editada`, com os campos alterados no motivo e o autor.

Qualificação (3.3: pergunta respondível, fonte candidata, duplicidade e playbook aplicável):

```bash
curl -X POST $API/acervo/ideias/$IDEIA/qualificacao -H "Authorization: Bearer $T_CARLA"
# {"qualificada": true, "pendencias": [], "candidatas_duplicidade": []}
```

Se vier `qualificada: false`, cada pendência traz uma etiqueta (`pergunta:`, `fonte:`,
`duplicidade:`, `playbook:`). Para uma candidata a duplicata, a curadora decide:

```bash
curl -X POST $API/acervo/ideias/$IDEIA/duplicidade -H "Authorization: Bearer $T_CARLA" \
  -H "Content-Type: application/json" -d '{"candidata_id": "<uuid>", "decisao": "distinta"}'
```

### 4.2 Ciclo de criação (responsável técnico)

O motor verifica uma a uma as 10 fontes candidatas (a semente só traz nomes: as que não tiverem URL
localizada ficam `nao_localizada`), redige a especificação e a critica por até 3 iterações.

```bash
curl --max-time 0 -X POST $API/criacao/ciclos -H "Authorization: Bearer $T_TATI"
# {"proposta_id": "<uuid>"}     (null se não há ideia qualificada livre)
```

**O ciclo inteiro roda dentro do pedido.** Com a API real, a verificação das 10 fontes (um agente
com busca web por fonte), a redação e a autocrítica levam dezenas de minutos. Use `curl` sem
limite de tempo (`--max-time 0`), não a Swagger UI (o navegador desiste antes). Se o cliente
desistir, o trabalho **continua no servidor** até o fim, gastando a API: confira o resultado depois
em `GET /criacao/propostas`. Para não prender um terminal, prefira o worker `motor-criacao`.

Quem dispara não pode aprovar a proposta. Para revisar:

```bash
curl "$API/criacao/propostas?status=aguardando_aprovacao" -H "Authorization: Bearer $T_ANA"
curl $API/criacao/propostas/$PROPOSTA -H "Authorization: Bearer $T_ANA"
# {"proposta": {...conteudo...}, "iteracoes": [...autocrítica...], "fontes": [...verificação...]}
```

### 4.3 Aprovação do projeto (dono) e responsáveis pelos gates

O dono decide a proposta. Ao aprovar, **designa a pessoa de cada papel de gate** (R13): sem isso a
aprovação é recusada (409). Os papéis de gate do playbook semente são `responsavel_por_dados` (M1),
`revisor_metodologico` (M3), `dono_do_projeto` (M5, o próprio dono, inserido automaticamente) e
`curador_do_acervo` (M6):

```bash
curl -X POST $API/criacao/propostas/$PROPOSTA/decisao -H "Authorization: Bearer $T_ANA" \
  -H "Content-Type: application/json" \
  -d '{"acao": "aprovar",
       "responsaveis": {"responsavel_por_dados": "davi",
                        "revisor_metodologico": "rita",
                        "curador_do_acervo": "carla"}}'
# {"status": "aprovada", "projeto_id": "<uuid>", ...}
```

Outras ações: `{"acao": "ajustar", "ajustes": ["..."]}` registra os ajustes pedidos e marca a
proposta como `ajustes_solicitados`, e `{"acao": "rejeitar", "motivo": "..."}` devolve a ideia à triagem.

O projeto nasce `aprovado`; o `worker-outbox` move a ideia para `em_projeto`.

### 4.4 Início da execução, com orçamento opcional (dono)

Sem corpo, vale o orçamento padrão (50 USD e 20 milhões de tokens; seção 6). Para fixar outro (R7),
informe qualquer um dos campos; os omitidos mantêm o valor atual:

```bash
curl -X POST $API/projetos/$PROJETO/execucao -H "Authorization: Bearer $T_ANA" \
  -H "Content-Type: application/json" \
  -d '{"max_tokens": 5000000, "max_custo_usd": "25.00", "prazo": "2026-12-31T23:59:00Z"}'
```

O worker cria uma tentativa por etapa pronta, executa o agente, o revisor valida a saída (até 3
tentativas por etapa) e, nas etapas com gate, o marco fica `aguardando_gate`. Acompanhe:

```bash
curl $API/projetos/$PROJETO/marcos -H "Authorization: Bearer $T_ANA"
curl $API/projetos/$PROJETO/artefatos -H "Authorization: Bearer $T_ANA"
```

Consumo (não há rota; use o banco como administrador):

```bash
docker compose exec db psql -U postgres -d ais4 -c "select * from execucao.consumo"
```

Estouro de orçamento, falhas repetidas de uma etapa e erros sistêmicos da API **pausam** o projeto
(`status: pausado`, com o motivo em `motivo_status`) e nunca seguem em silêncio. O dono decide:
retomar (com ajuste opcional do orçamento), ou cancelar:

```bash
curl -X POST $API/projetos/$PROJETO/retomada -H "Authorization: Bearer $T_ANA" \
  -H "Content-Type: application/json" -d '{"max_custo_usd": "40.00"}'
curl -X POST $API/projetos/$PROJETO/transicao -H "Authorization: Bearer $T_ANA" \
  -H "Content-Type: application/json" -d '{"para": "cancelado", "motivo": "fora do orçamento"}'
```

A retomada abre uma nova rodada de tentativas (o limite conta falhas seguidas desde a última
entrada em `em_execucao`; falha transitória da API e queda do worker, isto é, lease vencido ou
interrupção, não contam).

### 4.5 Gates M1, M3, M5 e M6

Cada gate é decidido pela pessoa designada em 4.3, e só com o projeto `em_execucao` ou
`em_revisao` (R14). Quem decidiu fica registrado no marco.

| Gate | Etapa | Decide |
| --- | --- | --- |
| M1 | coleta de dados | `responsavel_por_dados` (davi) |
| M3 | modelagem (projeções e cenários) | `revisor_metodologico` (rita) |
| M5 | relatório | `dono_do_projeto` (ana) |
| M6 | retroalimentação | `curador_do_acervo` (carla) |

O revisor de apoio (especialista de domínio; guardião no M6) é consultado fora do sistema: ele não
decide pela API. Para ler um artefato antes de decidir:
`GET /projetos/$PROJETO/artefatos/<aid>/conteudo` (confere o SHA-256 antes de entregar).

```bash
curl -X POST $API/projetos/$PROJETO/marcos/M1/decisao -H "Authorization: Bearer $T_DAVI" \
  -H "Content-Type: application/json" \
  -d '{"decisao": "aprovado", "comentario": "licenças e cobertura conferidas"}'
```

Com `"decisao": "reprovado"` e o comentário, a etapa é reaberta com esse feedback e o worker a
executa de novo (nova tentativa; a anterior é mantida).

### 4.6 Revisão (`em_revisao`) e retroalimentação 2.2

Quando todas as etapas e gates estão aprovados, o projeto vai sozinho a `em_revisao`. O processo
2.2 (disparado pelo `worker-outbox`) lê os artefatos `novas_ideias` e `licoes_aprendidas` da etapa
de retroalimentação e grava, sem chamar LLM:

- um próximo passo por item de `novas_ideias`; os de ação `ideia` viram ideias `rascunho` com
  `origem = retroalimentacao`, os de ação `descartar` ficam descartados com o motivo;
- uma lição por item de `licoes_aprendidas`; as de tipo `falhou` ou `sugestao` viram propostas de
  melhoria do playbook (tabela `playbooks.proposta_melhoria`, status `pendente`).

```bash
curl "$API/acervo/ideias?status=rascunho" -H "Authorization: Bearer $T_CARLA"
```

A curadora triagem as novas ideias como qualquer outra (`POST /acervo/ideias/{id}/transicao` com
`{"para": "triagem"}`) e, se quiser, pede o enriquecimento (tags, fontes, playbook sugerido):
`POST /acervo/ideias/{id}/enriquecimento`.

### 4.7 Conclusão (dono)

O banco só deixa concluir com ao menos uma lição, todos os próximos passos resolvidos (ideia
gerada ou descartado) e todos os gates aprovados. Quem revisa registra a avaliação (uma nota de 1 a
5 por critério; papéis dono, revisor metodológico ou especialista de domínio). Um projeto concluído
só conta para a promoção do playbook (4.9) se tiver **ao menos uma avaliação**:

```bash
curl -X POST $API/projetos/$PROJETO/conclusao -H "Authorization: Bearer $T_ANA"
curl -X POST $API/projetos/$PROJETO/avaliacoes -H "Authorization: Bearer $T_RITA" \
  -H "Content-Type: application/json" \
  -d '{"criterio": "rastreabilidade", "nota": 5, "comentario": "números rastreáveis"}'
```

O `worker-outbox` leva a ideia de origem a `concluida`. Se algum próximo passo não foi resolvido, a
conclusão é recusada (409); o descarte é feito pela curadora:

```bash
curl -X POST $API/projetos/$PROJETO/proximos-passos/<pid>/descarte \
  -H "Authorization: Bearer $T_CARLA" -H "Content-Type: application/json" \
  -d '{"motivo": "fora do escopo do piloto"}'
```

Não há rota que liste os próximos passos; para achar o `<pid>`:

```bash
docker compose exec db psql -U postgres -d ais4 -c \
 "select id, descricao from projetos.proximo_passo where projeto_id = '$PROJETO' and ideia_gerada_id is null and descartado_motivo is null"
```

### 4.8 Revisão periódica do acervo (processo 1.2)

Mensal, pela curadora (ou quem tem acesso ao servidor). Reindexa as ideias alteradas e imprime um
relatório JSON com candidatas à fusão e lacunas reabertas; não altera nenhuma ideia:

```bash
uv run python -m ais4 revisao-acervo
```

Fusões e arquivamentos são decididos pela curadora (`POST /acervo/ideias/{id}/transicao`).

### 4.9 Promoção do playbook

Um playbook `experimental` vira `validado` depois de **3 projetos concluídos**, cada um com nota
média **>= 4.0** (as notas vêm de `POST /projetos/{id}/avaliacoes`; projeto concluído sem nenhuma avaliação não é
contado). A promoção só se faz por esta rota: `POST .../status` com `"validado"` é recusado (409).
Com um só projeto concluído, o
pedido é recusado e a resposta diz o que falta:

```bash
curl -X POST $API/playbooks/projeto-academico-simples/versoes/0.1.0/promocao \
  -H "Authorization: Bearer $T_FRED"
# 409 {"erro": "Não elegível: 1 projetos concluídos com avaliação; o mínimo de 3 não foi atingido."}
```

Quando elegível (e a pessoa não é quem propôs a versão), a resposta traz a versão `validado` e a
elegibilidade (`projetos_concluidos`, `nota_media`). A spec (6.2) também pede revisão humana e teste
no projeto de referência: o sistema não verifica isso, é decisão do guardião e do revisor
metodológico antes de chamar a rota. Lições `falhou` e `sugestao` do piloto ficam em
`playbooks.proposta_melhoria` para a v0.2.0.

## 5. Se algo travar

| Sintoma | O que olhar |
| --- | --- |
| Ideia continua `qualificada` depois de aprovar | `worker-outbox` parado ou com erro no log |
| Projeto `pausado` | `GET /projetos/{id}` (`motivo_status`); tentativas esgotadas, orçamento ou erro da API |
| Etapa com Python não anda | Docker parado ou imagem `ais4-sandbox:0.1.0` ausente (o worker avisa no log) |
| Gate não aparece como decidível | O marco só fica `aguardando_gate` depois da etapa concluída e aprovada pelo revisor |
| 403 ao decidir um gate | A pessoa não é a designada em 4.3 (ou não tem o papel) |
| 409 ao aprovar a proposta | Falta designar alguém para algum papel de gate |
| 422 | Corpo ou parâmetro inválido. A resposta é `{"erro": "Requisição inválida: <campo>: <problema>; ..."}`, com todos os campos com problema |
| Processo não sobe, código de saída 3 | Classe de playbook editada sem nova versão: o log mostra os campos divergentes (seção 3) |
| Outbox com evento estacionado: um evento entre módulos não chega (ex.: a ideia não vai a `em_projeto`) e o log do `worker-outbox` mostra "estacionado" | O handler falhou 10 vezes. Ache os estacionados com `select id, tipo_evento, tentativas, ultimo_erro, estacionado_em from <schema>.outbox where estacionado_em is not null` (schemas `playbooks`, `acervo`, `projetos`, `criacao`, `execucao`). Corrija a causa (o erro está em `ultimo_erro`) e reprocesse com `update <schema>.outbox set estacionado_em = null, tentativas = 0 where id = <id>`; o `worker-outbox` o entrega na próxima volta |
| Ideia `qualificada` nunca é escolhida pelo motor | (1) Proposta órfã de um motor morto: é devolvida sozinha depois de 120 min sem atividade (`AIS4_PROPOSTA_ABANDONADA_MIN`); veja `GET /criacao/propostas?status=rascunho` e `?status=em_autocritica`. (2) A ideia voltou a `triagem` depois de 3 criações seguidas devolvidas: `GET /acervo/ideias/{id}` traz o motivo em `motivo_status`; corrija e requalifique. (3) Já há um projeto `em_execucao` (limite de 1) |
| Propostas `devolvidas` com motivo `api_transitoria: ...` | Instabilidade da API (429/5xx/529/conexão): o motor espera cada vez mais (até 30 min) e tenta de novo; não conta como falha da ideia |

## 6. Pontos em aberto e valores provisórios

Os valores estão em `ais4/config.py` (marcados `# PROVISÓRIO`) e podem ser trocados no `.env`.

| Ponto em aberto (spec, seção 10) | Valor provisório no piloto | Onde |
| --- | --- | --- |
| Limiares de promoção de playbook | 3 projetos concluídos, nota média >= 4.0 em cada um | `AIS4_PROMOCAO_MIN_PROJETOS`, `AIS4_PROMOCAO_NOTA_MIN` |
| Fórmula de prioridade do acervo | `prioridade = valor x viabilidade`, ajustável à mão (`prioridade_manual`) | `acervo.ideia` |
| Periodicidade do processo 1.2 | Mensal, disparado à mão (`revisao-acervo`); sem agendador | seção 4.8 |
| Limiar de duplicidade (1.2 e qualificação) | similaridade de cosseno >= 0.85 | `AIS4_LIMIAR_DUPLICIDADE` |
| Stack técnica | PostgreSQL 17 + pgvector; orquestrador próprio sobre o Postgres; Tool Runner do SDK `anthropic`; armazém em arquivos endereçados por SHA-256 (`file://`) | decidido |
| Pessoa por papel | Uma pessoa por papel, em `config/usuarios.toml` | seção 2.2 |
| Orçamento máximo por projeto | 50 USD e 20 milhões de tokens; prazo opcional | `AIS4_ORCAMENTO_PADRAO_USD`, `AIS4_ORCAMENTO_PADRAO_TOKENS` |
| Projetos em execução ao mesmo tempo | No máximo 1 | `AIS4_MAX_PROJETOS_EM_EXECUCAO` |
| Critério de "alimentos de qualidade" | **Ainda a definir.** A spec sugere a classificação por grau de processamento do Guia Alimentar (in natura e minimamente processados). **Precisa ser fixado na especificação antes da execução** | seção 4.1 |

Outros valores provisórios: autocrítica limitada a 3 iterações (`AIS4_MAX_ITERACOES_AUTOCRITICA`),
3 tentativas por etapa (`AIS4_MAX_TENTATIVAS_ETAPA`), preço da busca web de 0,01 USD por busca
(`AIS4_PRECO_BUSCA_WEB`) e todos os papéis de agente com o modelo `claude-opus-5-5` (troca por papel
em `AIS4_MODELO_POR_PAPEL`, ex.: `{"verificador_fontes": "..."}`).

Limites contra gasto e travamento sem fim (seção 3), também provisórios:

| O quê | Valor provisório | Onde |
| --- | --- | --- |
| Teto de custo por proposta do Motor de Criação | 5 USD | `AIS4_ORCAMENTO_PROPOSTA_USD` |
| Propostas seguidas devolvidas antes de a ideia voltar à triagem | 3 | `AIS4_MAX_DEVOLUCOES_IDEIA` |
| Falhas de handler antes de estacionar um evento do outbox | 10 tentativas | `AIS4_MAX_TENTATIVAS_EVENTO` |
| Tempo sem atividade para uma proposta ser dada como abandonada | 120 min | `AIS4_PROPOSTA_ABANDONADA_MIN` |

## 7. Limitações conhecidas do piloto

- **Um só `worker-outbox` por banco (R15).** Não escale esse processo no piloto (seção 3).
- **Sandbox em hosts Linux.** O container roda com `--user 1000:1000`. Num host Linux em que o
  usuário que sobe o `worker-execucao` não é o 1000, o container pode não conseguir gravar no
  workspace montado: ajuste o dono do diretório `workspaces/` ou use `Sandbox(usuario="uid:gid")` com o uid/gid do
  host. Não há variável de ambiente para isso: o `orquestrador.ciclo` cria `Sandbox()` com o padrão
  `1000:1000`, então trocar exige uma mudança de código ou injetar o sandbox. Não foi testado em
  Linux; o desenvolvimento foi em Windows com Docker Desktop.
- **O enriquecimento não tem orçamento.** As chamadas do enriquecimento 1.1 (rota sob demanda) são
  gravadas em `execucao.chamada_llm` com `ideia_id` (R30), mas não entram no orçamento de nenhum
  projeto nem na view `consumo` (que é por projeto). Para ver o gasto:
  `select ideia_id, sum(custo_usd) from execucao.chamada_llm where ideia_id is not null group by 1`.
- **Reentrada em `em_revisao`.** Se o dono devolve o projeto a `em_execucao` (revisão pede ajustes)
  e ele volta a `em_revisao` com outra retroalimentação, os próximos passos, ideias e lições de
  passagens anteriores continuam acumulados, sem marcar qual tentativa os produziu, e os itens são
  casados por posição e texto. Itens diferentes de uma nova tentativa somam-se aos antigos.
- **Próximo passo que o 2.2 não resolveu bloqueia a conclusão.** Se o 2.2 não conseguiu derivar uma
  ideia de um próximo passo (o erro fica no log do `worker-outbox`), o passo fica sem ideia e sem
  descarte, e a conclusão é recusada. Não há rota para reprocessar: resolva descartando o passo
  pela API (seção 4.7) e, se a ideia ainda interessa, crie-a à mão em `POST /acervo/ideias`.
- **Queda do `motor-criacao`.** Um worker interrompido (Ctrl+C, SIGTERM) devolve a proposta na
  hora. Um morto sem aviso (SIGKILL, OOM) deixa a proposta em andamento até ela completar 120 min
  sem atividade; só então a ideia volta a ser escolhida. Uma ideia "venenosa" (que sempre faz o
  ciclo falhar) custa no máximo 3 ciclos, cada um limitado a 5 USD, e volta à triagem.
- **Modelo de embeddings da Voyage.** O nome em `AIS4_EMBEDDING_MODELO` precisa ser confirmado
  contra a documentação atual da Voyage (1024 dimensões, multilíngue).
- **Preço com `usage.iterations`.** O cálculo de custo das respostas que usam `usage.iterations`
  (loops de ferramenta de servidor) ainda não foi verificado contra a API real; compare o custo
  registrado com o painel da Anthropic nos primeiros ciclos e ajuste `ais4/llm/custos.py` se divergir.
- **Sem interface web.** Os revisores usam a Swagger UI (`/docs`) ou `curl`; autenticação é por
  token em arquivo, sem OIDC.
