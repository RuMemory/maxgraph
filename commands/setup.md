---
description: Install the MaxGraph memory server and connect this agent to it
---

Set up MaxGraph persistent memory for the user. Do it yourself, step by step:

1. **Install the server** — pick one:
   - Docker (preferred): `git clone https://github.com/RuMemory/maxgraph && cd maxgraph && docker compose up -d --build`
   - Python 3.12+: clone the repo, then `pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"` and run `python app/app.py` from the repo root (set `MEMORY_DIR=./data`).
2. **Verify it's alive**: `curl http://localhost:8787/healthz` → `ok`.
3. **Get the access token** (auto-generated on first launch): `cat data/local_token.txt` — or `docker compose logs maxgraph | grep "автогенерация"`.
4. **Hand the token to the user**: ask them to open plugin settings (`/plugin` → maxgraph → configure) and paste it as `api_token`. The default URL `http://localhost:8787/mcp` needs no change.
5. **Verify memory works**: save a test fact with `create_entities`, read it back with `search_nodes`, then tell the user memory is live.

Docs: https://github.com/RuMemory/maxgraph/blob/master/docs/QUICKSTART.md
