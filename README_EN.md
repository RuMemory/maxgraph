<p align="center">
  <img src="docs/assets/banner.svg" alt="MaxGraph" width="440" />
</p>

<p align="center">
  <strong>Built-in memory remembers you. MaxGraph remembers all your projects.</strong>
</p>

<p align="center">
  <a href="https://rezon-ai.ru">Website</a> ·
  <a href="https://rezon-ai.ru/en/blog/">Blog</a> ·
  <a href="https://t.me/maxgraph_mcp">Telegram</a> ·
  <a href="#one-step-connection">Integrations</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-BSL--1.1-orange" alt="license" />
  <img src="https://img.shields.io/badge/windows%20%C2%B7%20macos%20%C2%B7%20linux-native-38d39f" alt="native" />
  <img src="https://img.shields.io/badge/MCP-streamable--http-ff5bd8" alt="MCP" />
</p>

<p align="center">
  <a href="README.md">Русский</a> · <strong>English</strong>
</p>

---

The memory built into ChatGPT and Claude remembers you: your name, tastes, habits. MaxGraph remembers your **work** — decisions, bugs, open tasks, session outcomes. Install once, and every agent you use gets the project context: Claude, Cursor, DeepSeek, and any other with MCP or REST support.

| Feature | What you get |
|---|---|
| 🧠 **Project memory** | Decisions, bugs and open tasks — dated, stored apart from the chat. Close the conversation, the records stay. |
| 🚀 **No warm-up start** | Every session opens with a short project brief: the agent is up to speed and never does the same work twice. |
| 🔍 **Semantic search** | Finds what matters by meaning, not by exact words. |
| 👀 **See everything** | Dashboard with a memory map, search and feed: check, edit or delete any record. |
| 🪶 **Token savings** | No re-reading the whole history — about −43 % tokens when working with MaxGraph. |

![MaxGraph dashboard: project memory map, recent records and event feed](docs/assets/demo-dashboard.png)

## How it works

Nothing new to learn: you work with your agent as usual, memory appears on its own.

1. **Work as usual.** The agent records the important parts of your work on its own: decisions, bugs, outcomes.
2. **Everything stays local.** Plain text files on your computer, no internet needed. The dashboard shows every record.
3. **New session — the agent is already up to speed.** It gets the project brief and picks up where you left off.

Memory is split into **projects**: work code, personal notes and freelance never mix.

## Installation

The easiest way — ask your AI agent. Paste this into its chat:

> Install MaxGraph: https://github.com/RuMemory/maxgraph — do everything yourself following AGENTS.md. Tell me when memory works.

The agent clones the repo, runs the installer, connects memory and verifies it is writing.

By hand — one command (no Docker needed):

```bash
git clone https://github.com/RuMemory/maxgraph && cd maxgraph
bash install.sh --yes       # Linux / macOS
```

```powershell
# Windows (PowerShell)
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes
```

From there the program runs itself: a tray icon, autostart with the system, automatic updates.

## One-step connection

Server already running? Then it's one command or one link:

- **Claude Code** — plugin from our marketplace, it will ask for the token during install:
  ```
  claude plugin marketplace add RuMemory/maxgraph
  claude plugin install maxgraph@maxgraph
  ```
- **Cursor** — paste into your browser's address bar (after install, replace `PASTE_TOKEN_HERE` with your token in Settings → MCP):
  ```
  cursor://anysphere.cursor-deeplink/mcp/install?name=maxgraph&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojg3ODcvbWNwIiwiaGVhZGVycyI6eyJBdXRob3JpemF0aW9uIjoiQmVhcmVyIFBBU1RFX1RPS0VOX0hFUkUifX0=
  ```
- **VS Code** — paste into your browser's address bar (the token goes into settings):
  ```
  vscode:mcp/install?%7B%22name%22%3A%22maxgraph%22%2C%22type%22%3A%22http%22%2C%22url%22%3A%22http%3A%2F%2Flocalhost%3A8787%2Fmcp%22%2C%22headers%22%3A%7B%22Authorization%22%3A%22Bearer%20PASTE_TOKEN_HERE%22%7D%7D
  ```
- **Cline and other agents** — hand the agent [AGENTS.md](AGENTS.md), it will install and connect everything itself.

Details for each client: [docs/PAIRING.md](docs/PAIRING.md).

## Works with

| Client | How to connect |
|---|---|
| Claude Desktop / Claude Code | Plugin from our marketplace, one command |
| Cursor / VS Code | One-click link, above |
| Cline / Continue | MCP streamable-http + Bearer |
| ChatGPT / OpenAI-compatible | REST API or agent adapter |
| Kimi, GLM, Qwen, Hermes, DeepSeek | REST API, any HTTP client |

Ready-made setup examples for each client: [docs/PROVIDERS.md](docs/PROVIDERS.md). Using a different agent? If it can connect external tools, MaxGraph will connect to it.

## Pricing

7 days free, no card. The trial starts on first launch, nothing to enable. After that, one plan with everything included, a one-time payment with no auto-renewal:

| Term | Price | Per month |
|---|---|---|
| 1 month | $5 | $5 |
| 3 months | $13 | ≈ $4.33 |
| 6 months | $24 | ≈ $4.00 |
| 12 months | $40 | ≈ $3.33 |

When the trial ends, only writing new facts stops working. The dashboard, search and export stay, and your accumulated memory goes nowhere.

Buy and activate: [rezon-ai.ru](https://rezon-ai.ru)

## Refer a friend

MaxGraph has a referral program. Your friend gets −20 % off the first purchase, and you get +10 % off your renewal for each buyer, up to −50 %. Your link and click stats are in the dashboard, under "System".

## Privacy

All records are stored only on your computer and work offline. They are plain text files in the `data/` folder: you can open, read and copy them at any time. The dashboard exports everything as a single file (NDJSON export).

## Links

- Site and purchase: [rezon-ai.ru](https://rezon-ai.ru)
- Blog: [rezon-ai.ru/en/blog](https://rezon-ai.ru/en/blog/) — articles on agent memory, MCP and token savings
- Telegram: [t.me/maxgraph_mcp](https://t.me/maxgraph_mcp) — news and releases (Russian)

Docs: [PROTOCOL](docs/PROTOCOL.md) · [API](docs/API.md) · [PROVIDERS](docs/PROVIDERS.md) · [PAIRING](docs/PAIRING.md) · [DEPLOY-LAN](docs/DEPLOY-LAN.md)

## Support

MaxGraph lives on user support. If the project was useful, you can simply say thanks: [rezon-ai.ru#donate](https://rezon-ai.ru/#donate). A one-time transfer, no subscription, no auto-charges.

## License

[BSL-1.1](LICENSE): the source code is open. Personal and internal use is free. Selling MaxGraph as a service requires a commercial license. Every version becomes MIT four years after publication.

## Keywords

`mcp server` `ai memory` `llm context` `knowledge graph` `persistent memory for ai agents` `model context protocol` `memory for claude code` `claude code plugin` `cursor memory` `vscode mcp` `chatgpt memory` `agent memory` `context engineering` `semantic search` `long-term memory for llm` `self-hosted ai` `память для нейросети` `память для ИИ-агентов` `MCP сервер памяти` `граф знаний` `долговременная память ИИ` `экономия токенов` `память для Claude` `память для ChatGPT` `память для Cursor` `локальная память для ИИ`

---

<p align="center">
  <strong>Install once — and every agent you use remembers the project.</strong>
</p>
