# 🔌 Providers — Maxgraph работает со всеми

Принцип: **один граф — все провайдеры**. MCP для нативных клиентов, REST для всего остального.

## Нативный MCP (streamable-http)

### Claude Desktop / Claude Code

`claude_desktop_config.json`:
```json
{ "mcpServers": { "maxgraph": {
  "url": "http://<host>:8787/mcp",
  "headers": { "Authorization": "Bearer <TOKEN>" } } } }
```

### Cursor / Cline / Continue

MCP settings → add server → type `streamable-http` (или `sse`/`http` в зависимости от версии) → URL `/mcp` + header `Authorization: Bearer …`.

### DeepSeek Harness (Cordis)

```yaml
- id: mcp-remotemem
  name: "@deepseek-ai/dsh-mcp-client"
  config:
    serverName: remotemem
    transport: streamable-http
    url: http://<host>:8787/mcp
    headers:
      Authorization: Bearer <TOKEN>
```

## REST для всех остальных

Любой агент с HTTP — Hermes, самописные GPT-агенты, Kimi, GLM, Qwen, n8n, Make:

```bash
# прочитать всё
curl -H "Authorization: Bearer $T" http://<host>:8787/api/graph

# найти
curl -H "Authorization: Bearer $T" "http://<host>:8787/api/search?q=deploy"

# добавить факт
curl -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"observations":[{"entityName":"project-x","contents":["релиз запланирован на пятницу"]}]}' \
  http://<host>:8787/api/observations
```

### OpenAI function calling / GPTs Actions

REST-эндпоинты Maxgraph мапятся на actions один-в-один: `search` → `GET /api/search`, `remember` → `POST /api/observations`, `recall` → `GET /api/graph`. OpenAPI-схема — в ROADMAP (v0.4).

## Локальные модели (Ollama, LM Studio, llama.cpp)

Maxgraph не требует интернета: ставится рядом с локальным рантаймом, агент ходит по LAN. См. [DEPLOY-LAN.md](DEPLOY-LAN.md).

## Контекстные профили провайдеров (Pro)

У каждого провайдера свой «слой» контекста: Claude помнит архитектурные решения, GPT — черновики текстов, Kimi — состояние DSH-окружения. Профили = namespaces в кабинете: `claude/`, `gpt/`, `kimi/`… с общим ядром `shared/`. Дизайн: [CABINET.md](CABINET.md).
