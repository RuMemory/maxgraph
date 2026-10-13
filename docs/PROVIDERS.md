# 🔌 Провайдеры — MaxGraph работает со всеми

Один граф памяти — для всех твоих агентов. MCP (streamable HTTP) для клиентов с поддержкой протокола, REST API для всего остального.

## MCP — нативно

Эндпоинт `http://<host>:8787/mcp`, заголовок `Authorization: Bearer <MEMORY_TOKEN>`.

### Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "maxgraph": {
      "url": "http://<host>:8787/mcp",
      "headers": { "Authorization": "Bearer <MEMORY_TOKEN>" }
    }
  }
}
```

### Cursor

Settings → MCP → новый сервер → тип `streamable-http` → тот же URL и Bearer-заголовок.

### DeepSeek Harness (DSH)

```yaml
- id: mcp-maxgraph
  name: "@deepseek-ai/dsh-mcp-client"
  config:
    serverName: maxgraph
    transport: streamable-http
    url: http://<host>:8787/mcp
    headers:
      Authorization: Bearer <MEMORY_TOKEN>
```

Плюс готовые client-plugin и SessionEnd-хук в `hooks/` — память пишется сама.

### Любой другой MCP-клиент

Gemini CLI, Kimi CLI, Windsurf, Cline, Continue — тот же URL и заголовок. Если клиент умеет MCP по HTTP — он совместим.

## REST — для всего остального

Kimi, GLM, Qwen, n8n, Make, самописные агенты — всё, что умеет HTTP:

```bash
T=<MEMORY_TOKEN>

# весь граф
curl -H "Authorization: Bearer $T" http://<host>:8787/api/graph

# поиск
curl -H "Authorization: Bearer $T" "http://<host>:8787/api/search?q=deploy"

# записать факт
curl -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"observations":[{"entityName":"project-x","contents":["релиз запланирован на пятницу"]}]}' \
  http://<host>:8787/api/observations

# полный экспорт (NDJSON)
curl -H "Authorization: Bearer $T" http://<host>:8787/api/export > backup.json
```

## ChatGPT / GPTs / OpenAI Actions

Готовая OpenAPI-схема для Actions: [openapi.yaml](openapi.yaml). Пошаговая инструкция: [GPTS.md](GPTS.md).

## Локальные модели (Ollama, LM Studio, llama.cpp)

MaxGraph не требует интернета: ставится рядом с локальным рантаймом, агент ходит по LAN. См. [DEPLOY-LAN.md](DEPLOY-LAN.md).

## Шпаргалка

| Провайдер | Путь подключения |
|---|---|
| Claude Code / Claude Desktop | MCP |
| Cursor / Windsurf / Cline / Continue | MCP |
| DeepSeek Harness | MCP + client-plugin + hook |
| Gemini CLI / Kimi CLI | MCP |
| ChatGPT | OpenAPI Actions (GPTs) |
| GLM / Qwen / n8n / Make / самописное | REST |
| Локальные модели | MCP или REST по LAN |
