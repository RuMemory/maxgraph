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

Блок в `~/.dsh/profiles/<профиль>/cordis.patch.yml`. Обёртка `- insert:` обязательна: без неё запись читается как правка существующего ряда и молча пропускается — инструменты не появятся.

```yaml
- insert:
    - id: mcp-maxgraph
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: maxgraph
        transport: streamable-http
        url: http://<host>:8787/mcp
        headers:
          Authorization: Bearer <MEMORY_TOKEN>
```

Плюс захват-хуки — память пишется сама: [HOOKS.md](HOOKS.md).

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

| Провайдер | Путь подключения | Захват-хуки |
|---|---|---|
| Claude Code / Claude Desktop | MCP | ✅ все события |
| DeepSeek Harness | MCP | ✅ все события |
| Cursor | MCP | ✅ все события |
| Kimi Code CLI | MCP | ✅ все события |
| Codex CLI | MCP | ✅ после одноразового доверия (`/hooks`) |
| Antigravity (Google) | MCP | ⚠️ урезано (нет SessionEnd/UserPromptSubmit) |
| Gemini CLI (legacy) | MCP | ✅ через аналоги событий |
| Windsurf / Devin | MCP | ⚠️ урезано (только промпт/ответ) |
| Cline / Continue | MCP | ❌ нет конфиг-хуков |
| ChatGPT | OpenAPI Actions (GPTs) | ❌ облако |
| GLM / Qwen / n8n / Make / самописное | REST | — |
| Локальные модели | MCP или REST по LAN | — |

Рецепты хуков под каждый хост — [HOOKS.md](HOOKS.md). Без хуков память всё равно
работает: агент ведёт её через MCP-инструменты (session_start → записи → session_close).
