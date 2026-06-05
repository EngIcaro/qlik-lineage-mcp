# Qlik Lineage MCP — Visão geral do projeto

Resumo técnico de como o MCP foi construído, quais endpoints usamos, a lógica de cada análise e o que ainda dá pra evoluir.

---

## 1. O que foi construído

Um servidor MCP **read-only** que expõe duas análises de lineage para tenants Qlik Cloud, especialmente úteis em contratos no modelo **capacity (pico de GB diário)**:

| Tool | Pergunta que responde |
|---|---|
| `ghost_files` | Quais data files de um espaço não são consumidos por nenhum app do tenant? (incluindo cadeias arquivo→app→arquivo) |
| `unused_columns` | Quais colunas de um data file não são consumidas por nenhum app do tenant? |

Tudo escrito em Python 3.10+, framework `FastMCP` (do SDK oficial `mcp`), `httpx` async, Pydantic, transporte stdio local. 40 testes passando contra fixtures reais capturadas via Postman.

---

## 2. Endpoints da API Qlik Cloud utilizados

Todos passam pelo `QlikClient` (`src/qlik_lineage_mcp/qlik_client.py`), que é a **única camada que toca a rede** no projeto:

| Endpoint | Para que serve | Detalhe importante |
|---|---|---|
| `GET /api/v1/spaces` | Resolver nome do espaço → `spaceId` | Paginado via `links.next.href` |
| `GET /api/v1/items?spaceId={id}&resourceType=app` | Listar apps em um espaço | |
| `GET /api/v1/items?resourceType=app` | Listar todos os apps do tenant | Usado pelas duas tools |
| `GET /api/v1/items?spaceId={id}&resourceType=dataset` | Listar data files (QVD/Parquet) em um espaço | Retorna `qri` (texto) e `secureQri` (hash) |
| `GET /api/v1/data-files?spaceId={id}` | Tamanho real em bytes de cada data file | Chamada em paralelo com `/items`; `/items` reporta `0` pra QVDs |
| `GET /api/v1/apps/{appId}/data/metadata` | Campos efetivamente carregados num app | Filtramos `is_system` e `is_hidden` |
| `GET /api/v1/apps/{appId}/data/lineage` | Discriminators que descrevem fontes/destinos do app | Strings free-form classificadas por regex |
| `GET /api/v1/lineage-graphs/nodes/{secureQri}` | Grafo de recurso (apps + datasets) — 1 hop | Mostra producers + sources upstream do nó |
| `GET /api/v1/lineage-graphs/nodes/{secureQri}?level=field` | Grafo de campo do mesmo nó | Field-level edges entre campos do nó e seus producers |
| `GET /api/v1/lineage-graphs/nodes/{appQri}?level=field` | Grafo de campo do **app consumer** | **Aqui** aparecem as renomeações: `file_field → app_field` |

### Endpoints que descartamos pelo caminho

- `GET /api/v1/lineage-graphs/impact/{qri}` — **não existe** sozinho (404)
- `GET /api/v1/lineage-graphs/impact/{qri}/actions/expand?node=...&level=...` — existe, mas **decompõe** o nó (file→table→fields). Não devolve downstream consumers.
- `GET /api/v1/apps/{appId}/script` — endpoint plural retorna histórico de versões, não o texto. Pra ter o código precisaria de duas chamadas (`/scripts` + `/scripts/{id}`). Foi por isso que abandonamos o parser de script (Plan A) em favor de só lineage de campo (Plan B).

---

## 3. Aprendizados técnicos (as armadilhas)

### 3.1 `qri` vs `secureQri`
O `/items` devolve dois campos pra cada arquivo:
- `qri` → texto claro (`qdf:qix-datafiles:tenantId:sid@spaceId:filename.qvd`) — só pra display
- `secureQri` → hash (`qri:qdf:space://<spaceHash>#<fileHash>`) — **o que a API de lineage aceita**

Mandar `qri` pra `/lineage-graphs/*` retorna **HTTP 400 genérico**. Memória salva pra futuras conversas: `~/.claude/projects/.../memory/qlik_lineage_qri_vs_securequri.md`.

### 3.2 Não existe endpoint single-call de impacto downstream
O `/nodes/{qri}` retorna só **upstream** (producers + sources). Pra descobrir quem consome um arquivo, **temos que iterar todos os apps do tenant** e ler `data/lineage` de cada um, procurando entries `lib://...{filename}`. É como o `ghost_files` já fazia; o `unused_columns` reusa o mesmo padrão.

### 3.3 Field-level lineage do **consumer** carrega o rename map
Quando você pede `/nodes/{app_qri}?level=field`, o grafo traz **transitivamente todo o upstream do app** (todos os QVDs lidos + dataprep producers + fontes DB). Edges com `source = file_field` e `target = app_field` representam:

- Leitura simples (`LOAD CAMPO FROM file.qvd`) → edge com mesmo label dos dois lados
- Renomeação (`LOAD ORIG AS ALIAS FROM file.qvd`) → edge `ORIG → ALIAS`
- **Expressão composta** (`LOAD CAMPO_A & '\\' & CAMPO_B AS CHAVE_COMPOSTA FROM file.qvd`) → Qlik decompõe em **duas edges** (`CAMPO_A → CHAVE_COMPOSTA` e `CAMPO_B → CHAVE_COMPOSTA`). Não precisamos parsear expressão. **Esse foi o achado mais bonito do projeto.**

Relations vistas em produção: `from`, `read`, `rename`, `modify`, `add`. Aceitamos qualquer uma como evidência.

### 3.4 Rate limit (HTTP 429)
Tenants grandes rate-limitam rajadas. Em um QVD com dezenas de consumers, várias chamadas field-level lineage podem falhar com 429.

Solução: `_get_with_retry` em `QlikClient` que honra `Retry-After` quando vem, senão faz backoff exponencial (cap 30s, 5 retries). Todas as chamadas GET passam por ele.

### 3.5 Match direto precisa ser **scoped a consumers** (o bug mais sutil)
Versão errada do `unused_columns` montava `direct_used = união dos campos de todos os apps do tenant`. Resultado contra um QVD real com 299 colunas: todas marcadas como usadas, zero unused. **Falso positivo enorme** — campos comuns aparecem em apps de teste, backups, demos e outros QVDs com nomes parecidos.

Versão correta: match direto **só conta** se a coluna aparece no metadata de algum dos **consumers identificados via `data/lineage`**. Após o fix, o mesmo QVD mostrou 26 used direct, 2 used_with_rename, **271 unused** — bate com a expectativa do desenvolvedor que conhece o arquivo (QVDs de camada bronze costumam ter muitos campos template que ninguém usa de fato).

---

## 4. Lógica de cada tool

### 4.1 `ghost_files`

```
Input: space_name
Phase 1: list_data_files_in_space(space)            → arquivos candidatos
Phase 2: para cada app do tenant:
            classifica data/lineage em LOAD/STORE   → bipartite graph (apps, files)
Phase 3: fixpoint:
            "leaf consumers" = apps que consomem mas não produzem
            files consumidos por consumers úteis    → úteis
            apps que produzem files úteis           → úteis
            repete até estabilizar
Output: arquivos do espaço que NÃO estão em úteis (incluindo cadeias ghost)
```

Comprovado em espaço real: arquivos com prefixo padrão de uma camada de transformação inteira marcados como ghost — confirmando que um modelo dimensional foi abandonado num refator anterior e ninguém limpou os QVDs órfãos.

### 4.2 `unused_columns` (3 fases)

```
Input: file_name, space_name
Phase 0: resolve space + target file via /items
Phase 1: GET /lineage-graphs/nodes/{file.secureQri}?level=field
            → file_columns = nodes com QRI prefixado pelo file QRI
Phase 2: para cada app do tenant:
            GET /apps/{id}/data/lineage
            classifica discriminators
            se algum disc é "load" do file.name → app é consumer
Phase 3: para cada CONSUMER:
            GET /apps/{id}/data/metadata        → field set do consumer
            match direto coluna→metadata        → used_direct (com app de evidência)
            GET /lineage-graphs/nodes/{app.lineage_qri}?level=field
            extrai edges source=file_field, target=app_field
            cada edge é uma renomeação        → used_with_rename
Phase 4: categoriza
            coluna ∈ used_direct                → "used" (evidência por-app)
            coluna ∈ rename_evidence            → "used_with_rename" (alias + relation)
            else                                → unused
Phase 5: response com sumário, listas com evidência, disclaimers
```

**Custo**: 1 + N + 2M chamadas (1 lineage do arquivo, N data/lineage de cada app, 2 chamadas por consumer M).
Em tenants com 2000 apps e 30 consumers: ~2061 chamadas. O retry de 429 mantém o sucesso ≈100%.

---

## 5. Estrutura modular (extensibilidade)

```
src/qlik_lineage_mcp/
├── server.py           # FastMCP entry point — chama register_all
├── config.py           # Settings (lê .env)
├── qlik_client.py      # ÚNICA camada HTTP; parsers + classify_discriminator
├── models.py           # Pydantic format-agnostic (DataFile = QVD ou Parquet)
└── tools/
    ├── __init__.py     # auto-registra todo módulo que exporta register(mcp)
    ├── unused_columns.py
    └── ghost_files.py
```

**Adicionar uma análise nova = criar 1 arquivo em `tools/` com `register(mcp)`.** O `server.py` nunca precisa ser tocado.

---

## 6. Validação real

Testado contra um tenant Qlik Cloud em produção com:

- Milhares de apps no tenant
- Dezenas de espaços
- `ghost_files`: identificou corretamente arquivos órfãos em um espaço de transformação (camada inteira abandonada)
- `unused_columns` em um QVD bronze com 299 colunas:
  - **26 usadas direto** (chaves de relacionamento e campos do domínio Ecommerce)
  - **2 com rename** (campos `X*` customizados pelo cliente sendo renomeados pra labels semânticos)
  - **271 unused** — campos template do ERP que nunca foram pra produção analítica (bases tributárias, parcelas de fundos, modalidades de cobrança, customizações antigas)
  - 100% dos consumers tiveram lineage extraído com sucesso após o retry

O resultado de 271 unused **bateu com a expectativa do desenvolvedor** que conhecia o arquivo — sinal de que a metodologia produz resultado acionável.

---

## 7. Próximos passos (roadmap)

### 7.1 Funcionalidades pendentes
- [x] **Tamanho real em GB**: `list_data_files_in_space` agora roda `/api/v1/items` e `/api/v1/data-files` em paralelo e faz merge por filename em `DataFile.estimated_size_bytes`.
- [ ] **Suporte Parquet validado**: o modelo é format-agnostic e tem TODO markers no código. Quando aparecer um fixture real (`/items` com Parquet), validar shape e remover os TODOs.
- [ ] **Cache compartilhado entre tools**: a varredura de `data/lineage` de todos os apps é cara e é feita tanto pelo `ghost_files` quanto pelo `unused_columns`. Cachear por sessão (TTL ~5min) reduz drasticamente o custo de chamar as duas em sequência.
- [ ] **Detecção de reload-staleness**: app cujo `lastReloadTime < lineage_activation_date` provavelmente tem lineage de campo vazio. Marcar esses apps como "suspeitos" na resposta antes mesmo de tentar puxar o grafo.

### 7.2 Novas análises (cada uma = 1 arquivo em `tools/`)
- [ ] **`duplicate_columns`**: mesma coluna semântica armazenada em vários QVDs (ex: mesma chave no BRONZE e no SILVER) — candidato a normalização.
- [ ] **`heavy_apps`**: apps que carregam muitos campos não usados nas visualizações (cruzar `data/metadata` com fields que aparecem em chart definitions — vai precisar de `/apps/{id}/objects`).
- [ ] **`stale_apps`**: apps cujo `lastReloadTime` é antigo + `usage: ANALYTICS` — candidatos a arquivar.
- [ ] **`circular_dependencies`**: chains arquivo→app→arquivo→app→... que formam ciclo (suspicious de erro de design).

### 7.3 Distribuição
- [ ] **Empacotar como DXT** (Desktop Extension) — Claude Desktop versões recentes (1.9659.x+ via Microsoft Store) usam DXT em vez do `claude_desktop_config.json` clássico. DXT = zip com `manifest.json` no padrão Anthropic. Permitiria instalar via "Adicionar plugins..." direto da UI, sem mexer em arquivo de config.
- [ ] **Publicar no PyPI** como `qlik-lineage-mcp` pra `pip install` simples.
- [ ] **Repo público no GitHub** com README focado em onboarding de outros devs Qlik.

### 7.4 Operação
- [ ] **Logging estruturado em JSON** com `traceId` que pode ser correlacionado com `X-Trace-ID` do Qlik (ele já devolve em todos os erros).
- [ ] **Métricas de tempo de execução** por fase (Phase 1/2/3 do `unused_columns`) — útil pra otimizar tenants grandes.
- [ ] **Testes de smoke periódicos** contra um tenant real, fora dos fixtures, pra detectar quebra de API.

---

## 8. Limitações conhecidas (honestidade técnica)

1. **Rename detection depende de lineage de campo populado**: apps consumer que não foram recarregados desde a ativação do lineage no tenant não aparecem no grafo com edges, e renomeações neles ficam invisíveis. O output lista esses apps em `consumer_lineage_failures` pra deixar claro.

2. **`data/lineage` ignora SUB/CALL/$(include)**: arquivos referenciados via indireção de script não são capturados como consumo. Disclaimer no output.

3. **Match direto é case-insensitive mas não decompõe**: se um app tem `[Cliente Completo]` no metadata e o QVD tem `Cliente_Completo` — não vai bater. Vale registrar.

4. ~~**Tamanho em GB ainda é placeholder**~~ — resolvido: `estimated_size_bytes` agora vem de `/api/v1/data-files` (chamada paralela). Arquivos deletados entre as duas chamadas ficam com `0`.

5. **Parquet é melhor esforço**: format detection olha `resourceAttributes.type == 'parquet'` (case-insensitive), mas precisamos de um fixture real pra validar o resto da pipeline.

---

## 9. Arquivos importantes do repo

| Arquivo | O que tem |
|---|---|
| `src/qlik_lineage_mcp/qlik_client.py` | Camada HTTP, parsers, `classify_discriminator`, `_get_with_retry` |
| `src/qlik_lineage_mcp/models.py` | Pydantic models, `App.lineage_qri`, `FileFormat` enum |
| `src/qlik_lineage_mcp/tools/unused_columns.py` | Pipeline de 3 fases |
| `src/qlik_lineage_mcp/tools/ghost_files.py` | Bipartite + fixpoint |
| `tests/fixtures/*.json` | Respostas reais capturadas via Postman do tenant de teste |
| `tests/test_unused_columns.py` | Inclui integration test contra fixtures reais de consumer com expressão composta |
| `.mcp.json` | Configuração local pra Claude Code apontar pro servidor |
| `guideline-mcp-qlik-data-lineage.md` | Brief original do projeto (em português) |
| `PROJECT_OVERVIEW.md` | Este arquivo |

---

_Última atualização: 2026-06-05._
