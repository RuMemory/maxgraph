![MaxGraph — Your AI forgets. MaxGraph remembers.](docs/assets/banner.jpg)

# 🌌 MaxGraph

🇷🇺 [Русская версия](README.md) · English

## Stop explaining everything to your AI from scratch

MaxGraph is memory for working with AI. It remembers your project: what you decided, what broke, what was promised. Next session, your agent finds it on its own and picks up where you left off.

Works with Claude, ChatGPT, Cursor, Kimi and DeepSeek. Using a different agent? If it can connect external tools, MaxGraph will connect to it — see the table below.

🌐 Site & purchase: [rezon-ai.ru](https://rezon-ai.ru) · 7-day trial, no card · ~43 % token savings

![license](https://img.shields.io/badge/license-BSL--1.1-orange) ![docker](https://img.shields.io/badge/docker-ready-38d39f) ![MCP](https://img.shields.io/badge/MCP-streamable--http-ff5bd8)

![MaxGraph dashboard: project memory map, recent memories and memory feed](docs/assets/demo-dashboard.png)

## What you get

- Your agent remembers the project between sessions: decisions, bugs, tasks and agreements — with dates.
- Records live separately from the chat. Close or delete the conversation, the records stay.
- Every new session starts from a short project brief. No re-reading the whole history — that's where the token savings come from.
- Unfinished business resurfaces on its own, no reminders needed.
- Everything recorded is visible in the dashboard: memory map, search, event feed.

## Install (2 minutes)

You only need Docker:

```bash
git clone https://github.com/RuMemory/maxgraph && cd maxgraph
docker compose up -d --build
```

The access token is generated automatically on first launch — grab it from the logs:

```bash
docker compose logs maxgraph | grep "автогенерация"   # or: cat data/local_token.txt
```

Open the dashboard at `http://localhost:8787/app` and paste the token. Connecting your agent: [docs/QUICKSTART.md](docs/QUICKSTART.md).

Updates install themselves, once a day. Don't want them — disable with `AUTO_UPDATE=0`.

## Works with

| Client | How to connect |
|---|---|
| Claude Desktop / Claude Code | MCP config → `http://host:8787/mcp` |
| Cursor / Cline / Continue | MCP streamable-http + Bearer |
| DeepSeek Harness | `dsh-mcp-client`, see [PROVIDERS](docs/PROVIDERS.md) |
| ChatGPT / OpenAI-compatible | REST API or an agent adapter |
| Kimi, GLM, Qwen, Hermes… | REST API — any HTTP client |

Ready-made setup examples for each client: [docs/PROVIDERS.md](docs/PROVIDERS.md)

### Connect in one step

Server already running (see «Install» above)? Then it's one command or one link:

- **Claude Code** — plugin from our marketplace (asks for the token during install):
  ```
  claude plugin marketplace add RuMemory/maxgraph
  claude plugin install maxgraph@maxgraph
  ```
- **Cursor** — paste into your browser's address bar (after installing, replace `PASTE_TOKEN_HERE` with your token in Settings → MCP):
  ```
  cursor://anysphere.cursor-deeplink/mcp/install?name=maxgraph&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojg3ODcvbWNwIiwiaGVhZGVycyI6eyJBdXRob3JpemF0aW9uIjoiQmVhcmVyIFBBU1RFX1RPS0VOX0hFUkUifX0=
  ```
- **VS Code** — paste into your browser's address bar (token goes into settings):
  ```
  vscode:mcp/install?%7B%22name%22%3A%22maxgraph%22%2C%22type%22%3A%22http%22%2C%22url%22%3A%22http%3A%2F%2Flocalhost%3A8787%2Fmcp%22%2C%22headers%22%3A%7B%22Authorization%22%3A%22Bearer%20PASTE_TOKEN_HERE%22%7D%7D
  ```
- **Cline and other agents** — hand the agent [llms-install.md](llms-install.md); it installs and connects everything itself.

## Pricing

**7 days free, no card** — the trial starts on first launch, nothing to enable. After that, one all-inclusive plan, one-time payment, no auto-renewal:

| Period | Price | Per month |
|---|---|---|
| 1 month | $1.99 | $1.99 |
| 3 months | $4.99 | ≈ $1.66 |
| 6 months | $9.99 | ≈ $1.67 |
| 12 months | $14.99 | ≈ $1.25 |

The site switches to English on its own (RU/EN toggle in the header).

When the trial ends, only writing new facts stops. The dashboard, search and export keep working — the memory you've built isn't going anywhere.

Buy & activate: [rezon-ai.ru](https://rezon-ai.ru)

## 💛 Support

MaxGraph runs on user support. If the project has been useful, you can simply say thanks: [rezon-ai.ru#donate](https://rezon-ai.ru/#donate). One-time payment, no subscription.

## Privacy

All records are stored only on your computer. They are plain text files in the `data/` folder: you can open, read and copy them at any time.

## License

[BSL-1.1](LICENSE): source-available. Personal and internal use is free. Offering MaxGraph as a service requires a commercial license. Every version becomes MIT four years after release.

## Docs

[QUICKSTART](docs/QUICKSTART.md) · [PROTOCOL](docs/PROTOCOL.md) · [API](docs/API.md) · [GPTs](docs/GPTS.md) · [PROVIDERS](docs/PROVIDERS.md) · [PAIRING](docs/PAIRING.md) · [DEPLOY-LAN](docs/DEPLOY-LAN.md)

## Keywords

`mcp server` `ai memory` `llm context` `knowledge graph` `claude memory` `persistent memory for ai agents` `model context protocol` `self-hosted ai` `agent memory` `context engineering` `semantic search` `long-term memory for llm` `память для нейросети` `память для ИИ-агентов` `MCP сервер памяти` `граф знаний` `долговременная память ИИ` `экономия токенов контекста` `память для Claude` `память для ChatGPT` `память для DeepSeek` `локальная память для ИИ`
