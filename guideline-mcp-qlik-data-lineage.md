# Guideline: Como construir um MCP (caso de uso — Qlik Cloud Data Lineage)

> Documento de referência para o meu **primeiro MCP** e para os próximos.
> Cada passo tem uma seção **"Por que isso é necessário"** para eu não decorar receita, e sim entender a lógica.
> Convenções usadas no doc:
> - 🧑‍💻 **VOCÊ (eu)** = passo manual meu (ou decisão de negócio/credencial)
> - 🤖 **CLAUDE** = passo que eu delego ao Claude Code (escrever/refatorar código)
> - 🤝 **JUNTOS** = iteração lado a lado (eu testo, ele ajusta)

---

## 0. O que é um MCP, em uma frase (para fixar o conceito)

MCP (Model Context Protocol) é um padrão aberto, baseado em **JSON-RPC 2.0**, que padroniza como um modelo de IA (host/cliente) descobre e chama ferramentas e dados externos (servidor). A analogia oficial é a do **"USB-C para IA"**: em vez de escrever uma integração customizada para cada par modelo×ferramenta, você expõe suas ferramentas uma vez via um servidor MCP, e qualquer host compatível (Claude Desktop, Claude Code, VS Code, etc.) consegue usá-las.

Um servidor MCP expõe três tipos de primitivas:

| Primitiva | Analogia HTTP | Uso no nosso projeto Qlik |
|---|---|---|
| **Tool** | POST (executa ação / efeito) | `analisar_colunas_nao_usadas`, `detectar_qvd_fantasma` |
| **Resource** | GET (carrega dado no contexto) | Listar espaços, listar QVDs de um espaço |
| **Prompt** | Template reutilizável | Ex.: "relatório de otimização de capacity" |

**Por que isso é necessário:** entender que **Tool = ação** e **Resource = leitura** evita o erro clássico de iniciante de transformar tudo em "tool". No nosso caso, "listar QVDs de um espaço" é leitura (Resource ou tool de consulta), enquanto "analisar e recomendar remoção de coluna" é uma ação analítica (Tool).

---

## 1. Pré-requisitos e decisões de arquitetura

### 1.1 🧑‍💻 VOCÊ — Definir linguagem e stack
**Decisão recomendada:** Python + **FastMCP** (o framework de fato; o FastMCP 1.0 foi incorporado ao SDK oficial `mcp`, e a linha standalone é a mais usada hoje).

- Python ≥ 3.10
- Gerenciador `uv` (instala dependências muito mais rápido que pip e é o padrão da comunidade MCP) — `pip` + venv também funciona.

**Por que isso é necessário:** você já vive no ecossistema de dados; Python tem as melhores libs para tratar o JSON de lineage e fazer as análises de conjunto (interseção de campos). FastMCP gera schema, validação e negociação de transporte automaticamente, então você escreve só a lógica.

### 1.2 🧑‍💻 VOCÊ — Provisionar acesso ao Qlik Cloud
Antes de qualquer código, garanta:
- URL do tenant (ex.: `https://SEU-TENANT.REGIAO.qlikcloud.com`)
- Uma **API Key** (ou OAuth M2M) com permissão de leitura sobre apps, espaços e lineage.

**Por que isso é necessário:** o MCP é só uma "casca" que orquestra chamadas autenticadas à API do Qlik. Sem credencial válida, nenhuma tool funciona. **A credencial é responsabilidade sua e nunca entra no código** (vai em variável de ambiente / `.env`).

---

## 2. Levantamento de API do Qlik (a parte mais sua)

> Esta é exatamente a divisão que você intuiu na pergunta: **o conhecimento de domínio Qlik é seu**. Eu não tenho acesso ao seu tenant, então você é minha fonte de verdade sobre o formato real das respostas.

### 2.1 🧑‍💻 VOCÊ — Mapear os endpoints na documentação
Endpoints relevantes que já levantei para o seu caso (confirme na doc, pois a API evolui):

| Objetivo | Endpoint | Observação |
|---|---|---|
| Listar espaços | `GET /api/v1/spaces` | Para resolver "nome do espaço" → `spaceId` |
| Listar apps/itens de um espaço | `GET /api/v1/items?spaceId=...` | Para varrer o que existe no espaço |
| Metadados de dados de um app (tabelas+campos efetivamente carregados) | `GET /api/v1/apps/{appId}/data/metadata` | **Chave** para saber quais campos cada app realmente usa |
| Lineage de um app | `GET /api/v1/apps/{appId}/data/lineage` | Mostra de onde vem o dado (QVD, RESIDENT, etc.). ⚠️ tem limitações conhecidas: tabelas dropadas no script podem não aparecer |
| Lineage graph / impacto (nível campo) | `GET /api/v1/lineage-graphs/nodes/{id}?level=field` e `/lineage-graphs/impact/{id}/...` | Grafo upstream/downstream; use `up=-1` p/ todas as camadas, `collapse=false` p/ expandir nós internos |
| Itens de dados / QVD no catálogo | endpoints de `data-files` / catálogo | Para localizar o QVD físico e seu espaço |

**Por que isso é necessário:** a análise de "coluna não usada" e "QVD fantasma" é, no fundo, um problema de **grafo de dependências**. Você precisa saber *quais* endpoints devolvem (a) os campos que existem num QVD e (b) os campos/QVDs efetivamente consumidos por cada app. A diferença entre esses conjuntos é a sua resposta.

> ⚠️ Limitação importante a registrar: o lineage do Qlik **nem sempre captura 100%** (ex.: campos renomeados, tabelas dropadas, scripts complexos). Documente isso como "margem de erro" da ferramenta — é honestidade técnica que protege o cliente capacity de uma decisão errada de remoção.

### 2.2 🧑‍💻 VOCÊ — Testar no Postman e salvar amostras de retorno
Para **cada** endpoint que vamos usar:
1. Chame no Postman com a sua API Key.
2. Salve o JSON de resposta real em arquivos (ex.: `samples/apps_data_metadata.json`, `samples/lineage_app_X.json`).
3. Anote casos de borda: app sem lineage, espaço vazio, QVD usado em 0 apps, campo com nome divergente entre QVDs.

**Por que isso é necessário (crítico):** eu **não consigo ver seu tenant**. Se você me der o **shape real** do JSON, eu escrevo o parser certo de primeira. Sem amostra, eu teria que adivinhar a estrutura e o código viria errado. As amostras também viram seus **fixtures de teste** (item 6) — então você testa a lógica offline, sem gastar chamada de API nem capacity.

### 2.3 🧑‍💻 VOCÊ — Confirmar as regras de negócio das análises
Responda por escrito (vira a spec das tools):
- "Coluna não usada" = campo presente no QVD de origem que **não aparece** no `data/metadata` de **nenhum** app do tenant? Ou só dos apps de um conjunto de espaços?
- Comparação de campos é **case-sensitive**? Considera renomeações (`AS`)?
- "QVD fantasma" = QVD que não é `source` em nenhum lineage de app? Inclui QVDs lidos por outros QVDs (cadeia QVD→QVD)?

**Por que isso é necessário:** essas definições mudam completamente o algoritmo. Travar a regra antes evita retrabalho e evita que a ferramenta recomende apagar algo que era usado de um jeito que não foi modelado.

---

## 3. Estrutura modular do projeto (a base que garante extensibilidade)

> Seu requisito explícito: **adicionar uma nova tool/análise tem que ser fácil**. Isso se resolve na arquitetura, não na hora de programar a tool.

### 3.1 🤖 CLAUDE — Gerar o scaffold modular
Estrutura proposta:

```
qlik-lineage-mcp/
├── pyproject.toml            # deps e metadados (uv/pip)
├── .env.example              # modelo de variáveis (SEM segredos reais)
├── README.md
├── src/
│   └── qlik_lineage_mcp/
│       ├── __init__.py
│       ├── server.py         # cria o FastMCP e REGISTRA os módulos
│       ├── config.py         # lê env vars (tenant, api key)
│       ├── qlik_client.py    # camada HTTP: 1 lugar só que fala com a API Qlik
│       ├── models.py         # dataclasses/pydantic do shape do Qlik
│       └── tools/            # <<< cada análise é um arquivo aqui
│           ├── __init__.py   # auto-registro (register_all)
│           ├── unused_columns.py
│           └── ghost_qvds.py
├── tests/
│   └── fixtures/             # os JSON que VOCÊ salvou do Postman
└── samples/
```

**Por que essa estrutura garante modularidade:**
- **`qlik_client.py` isolado**: toda autenticação e chamada HTTP fica num único lugar. Se a API mudar, você muda 1 arquivo, não 10. Nova tool nunca reescreve auth.
- **Pasta `tools/` com auto-registro**: adicionar análise = criar 1 arquivo novo + ele se registra sozinho. O `server.py` não precisa ser editado a cada tool nova → é literalmente "plugar".
- **`models.py` separado**: o shape do Qlik fica num lugar; as tools trabalham com objetos tipados em vez de cavar dicionário cru — menos bug.
- **`tests/fixtures`**: testa lógica sem tocar no tenant (protege capacity).

### 3.2 🤖 CLAUDE — Implementar o padrão de auto-registro
Cada tool exporta uma função `register(mcp)` e o `tools/__init__.py` percorre o pacote e chama todas. Assim, "nova análise" = criar arquivo + escrever a função. **Por que:** elimina o ponto único de edição manual (o registro central), que é onde projetos viram bagunça com o tempo.

---

## 4. Implementação das tools (núcleo da colaboração)

### 4.1 🤝 JUNTOS — Camada de cliente Qlik
1. 🤖 Eu escrevo `qlik_client.py` (métodos: `list_spaces`, `list_items`, `get_app_metadata`, `get_lineage`, etc.) baseado nas **suas amostras** do item 2.2.
2. 🧑‍💻 Você roda contra o tenant real e me devolve erros/diferenças.
3. 🤖 Eu ajusto.

**Por que iterativo:** doc de API e realidade divergem. As suas amostras me levam a 90%; o restante é ajuste fino com retorno real.

### 4.2 🤝 JUNTOS — Tool "colunas não usadas"
Lógica (independente de framework):
1. Resolver `espaço` + `nome do QVD` → localizar o QVD e ler seus campos.
2. Montar o conjunto `campos_no_qvd`.
3. Varrer apps (do escopo definido em 2.3), coletar `campos_usados` via `data/metadata`/lineage.
4. `candidatos_remocao = campos_no_qvd - campos_usados`.
5. Retornar lista + avisos de incerteza (renomeações, lineage incompleto).

### 4.3 🤝 JUNTOS — Tool "QVD fantasma"
1. Listar QVDs do espaço.
2. Para cada QVD, checar se aparece como `source` em algum lineage de app (e em cadeias QVD→QVD).
3. Retornar QVDs com 0 consumidores + tamanho/ganho potencial de GB.

**Por que retornar "ganho potencial de GB":** seu público é cliente **capacity** com pico diário de GB. A ferramenta fica muito mais acionável se já traduzir o achado em economia estimada.

### 4.4 🧑‍💻 VOCÊ — Validar os resultados contra um caso que você conhece
Rode as tools num espaço onde você **já sabe a resposta** e confira.
**Por que:** é o único jeito de calibrar a margem de erro do lineage antes de confiar a ferramenta a um cliente.

---

## 5. Rodar e depurar localmente

### 5.1 🤝 JUNTOS — MCP Inspector
Subir o servidor e abrir o **MCP Inspector** para chamar cada tool manualmente e ver entrada/saída antes de plugar num cliente.
**Por que:** o Inspector isola "o problema é a minha lógica?" de "o problema é a integração com o host?". Depurar as duas coisas ao mesmo tempo é sofrimento.

### 5.2 🧑‍💻 VOCÊ — Conectar ao cliente (Claude Desktop / Claude Code / VS Code)
Adicionar o servidor à config do cliente apontando para o comando que sobe o `server.py`.
**Por que:** só depois de funcionar no Inspector vale a pena conectar ao host real — aí você usa em linguagem natural ("analise o espaço Vendas").

---

## 6. Testes e qualidade

### 6.1 🤖 CLAUDE — Testes com os fixtures
Eu escrevo testes que rodam a lógica de "colunas não usadas" / "QVD fantasma" contra os JSON salvos, **sem chamar a API**.
**Por que:** garante que refatorar (ou eu adicionar tool) não quebra as análises existentes. E não consome capacity do cliente em cada teste.

### 6.2 🧑‍💻 VOCÊ — Teste de fumaça contra o tenant real (periódico)
**Por que:** fixtures envelhecem quando a API muda. Um teste real ocasional pega regressão de API.

---

## 7. Segurança e operação (não pular)

- 🧑‍💻 **VOCÊ:** segredos só em `.env` / variável de ambiente; `.env` no `.gitignore`; commitar apenas `.env.example`.
  **Por que:** API Key de Qlik vazada = acesso ao tenant do cliente. Isso é incidente de segurança, não bug.
- 🤖 **CLAUDE:** acrescentar tratamento de erro/rate-limit no `qlik_client` e logs sem dados sensíveis.
- 🧑‍💻 **VOCÊ:** decidir se cada tool é **read-only**. Para data lineage, **recomendo manter tudo só-leitura** — a ferramenta *recomenda* remoção, mas **não apaga** QVD nem altera app.
  **Por que:** sugerir é reversível; apagar QVD com base em lineage incompleto pode quebrar a recarga de um cliente. Mantenha o humano no controle da ação destrutiva.
- 🧑‍💻 **VOCÊ (futuro):** se for expor remoto (HTTP em vez de stdio local), aí entram OAuth 2.1, TLS e timeouts de proxy.
  **Por que:** stdio local é seguro por padrão (roda na sua máquina); remoto expõe a rede e exige autenticação real.

---

## 8. Checklist resumido (divisão de responsabilidades)

| # | Passo | Responsável |
|---|---|---|
| 1 | Escolher stack (Python + FastMCP, uv) | 🧑‍💻 VOCÊ |
| 2 | Conseguir tenant URL + API Key | 🧑‍💻 VOCÊ |
| 3 | Mapear endpoints na doc Qlik | 🧑‍💻 VOCÊ |
| 4 | Testar no Postman + salvar JSON de retorno | 🧑‍💻 VOCÊ |
| 5 | Travar regras de negócio das análises | 🧑‍💻 VOCÊ |
| 6 | Gerar scaffold modular + auto-registro | 🤖 CLAUDE |
| 7 | Escrever `qlik_client.py` a partir das amostras | 🤖 CLAUDE |
| 8 | Implementar tools (colunas/QVD fantasma) | 🤝 JUNTOS |
| 9 | Validar resultado contra caso conhecido | 🧑‍💻 VOCÊ |
| 10 | Depurar no MCP Inspector | 🤝 JUNTOS |
| 11 | Conectar ao Claude Desktop/Code | 🧑‍💻 VOCÊ |
| 12 | Testes com fixtures | 🤖 CLAUDE |
| 13 | Segurança (.env, read-only) | 🧑‍💻 VOCÊ |

---

## 9. Como adicionar uma nova análise no futuro (o "para lembrar depois")

1. 🧑‍💻 Definir a regra de negócio + salvar amostra de qualquer endpoint novo no Postman.
2. 🤖 (via Claude Code) Criar `src/qlik_lineage_mcp/tools/minha_nova_analise.py` com uma função `register(mcp)` e a `@mcp.tool()`.
3. Pronto — o auto-registro pluga sozinho. Sem editar `server.py`.
4. 🤖 Adicionar fixture + teste.
5. 🧑‍💻 Validar no Inspector.

**Por que isso fecha o seu requisito de modularidade:** o custo de uma análise nova é "1 arquivo + 1 teste", e nunca mexer na infraestrutura (auth, registro, transporte). É isso que mantém o projeto saudável ao longo de 7+ anos de uso, como você está acostumado a manter ambientes Qlik.

---

### Sobre o Claude Code no VS Code — sim, é o caminho certo
Sua intuição está correta: usar o Claude Code (no VS Code ou no terminal) para ir construindo os arquivos dinamicamente é a forma recomendada. Ele cria/edita os arquivos do scaffold, escreve as tools e os testes, e você itera comigo em tempo real. A divisão prática: **você traz domínio Qlik + credenciais + amostras de API + validação de negócio; eu escrevo e refatoro o código.**

---

## Referências consultadas
- MCP — visão geral e primitivas (Tools/Resources/Prompts): qlik.dev e docs MCP, protocolo rev. 2025-11-25.
- SDK Python oficial / FastMCP: github.com/modelcontextprotocol/python-sdk e gofastmcp.com.
- Qlik Cloud — Lineage graphs REST: qlik.dev/apis/rest/lineage-graphs/ e qlik.dev/manage/get-started-lineage/.
- Qlik Cloud — app data metadata/lineage: qlik.dev/apis/rest/apps/.
- Limitações conhecidas de lineage: Qlik Community (GET /v1/apps/{appId}/data/lineage).
