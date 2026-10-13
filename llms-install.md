# MaxGraph — installation instructions for AI agents

This file is written for AI agents (Cline, Cursor, Claude Code and others) that install
software on behalf of the user. Follow it top to bottom; every step is required unless
marked optional.

## What you are installing

MaxGraph is a self-hosted persistent-memory server for AI agents. One container (or one
Python process) provides:

- MCP endpoint (streamable HTTP): `http://localhost:8787/mcp`
- REST API: `http://localhost:8787/api/*`
- Dashboard: `http://localhost:8787/app`

Authentication: `Authorization: Bearer <token>` header. The token is auto-generated on
first launch and stored in `data/local_token.txt`.

## Prerequisites

- Preferred: Docker with the compose plugin.
- Alternative: Python 3.12+ (pip).

## Step 1 — install

Docker (preferred):

```bash
git clone https://github.com/RuMemory/maxgraph.git
cd maxgraph
docker compose up -d --build
```

Python (no Docker):

```bash
git clone https://github.com/RuMemory/maxgraph.git
cd maxgraph
pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"
python app/app.py        # run from the repo root; data lands in ./data (set MEMORY_DIR to override)
```

Windows PowerShell equivalent for the Python path: set `$env:MEMORY_DIR="./data"`, then
`python app/app.py` from the repo root.

## Step 2 — verify the server is alive

```bash
curl http://localhost:8787/healthz
# expected: ok
```

## Step 3 — get the access token

The token is created automatically on first launch (`mg_local_…`):

```bash
cat data/local_token.txt
# or, for Docker:
docker compose logs maxgraph | grep "автогенерация"
```

## Step 4 — connect the MCP client

Add this MCP server to the client's configuration:

```json
{
  "mcpServers": {
    "maxgraph": {
      "type": "http",
      "url": "http://localhost:8787/mcp",
      "headers": { "Authorization": "Bearer <TOKEN_FROM_STEP_3>" }
    }
  }
}
```

Client-specific locations:

- **Claude Code / Claude Desktop**: `mcpServers` in the client config (`claude_desktop_config.json`
  or `claude mcp add`); plugin users: `claude plugin marketplace add RuMemory/maxgraph` →
  `claude plugin install maxgraph@maxgraph`, then paste the token when prompted.
- **Cursor**: Settings → MCP (`mcpServers` block above).
- **Cline**: MCP settings → paste the same block.
- **Other agents**: any HTTP client works — REST API (`/api/search`, `/api/observations`, …)
  documented in `docs/API.md`.

## Step 5 — verify memory works end to end

1. Save a test fact: tool `create_entities` — e.g. entity `install-check`, fact
   "MaxGraph installed on <today's date>".
2. Read it back: tool `search_nodes` with query `install-check` must return it.
3. Tell the user: memory is live, dashboard at `http://localhost:8787/app` (log in with the
   same token).

## If something fails

- `healthz` not answering → container not running: `docker compose ps`, `docker compose logs maxgraph`.
- MCP calls return 401 → wrong token; re-read `data/local_token.txt` (it regenerates if `data/`
  was wiped).
- More: `docs/QUICKSTART.md`, `docs/PAIRING.md` (buyer flow), `docs/PROVIDERS.md` (per-client
  configs), `docs/DEPLOY-LAN.md` (LAN/offline).
