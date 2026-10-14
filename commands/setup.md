---
description: Install the MaxGraph memory server and connect this agent to it
---

Set up MaxGraph persistent memory for the user. Do it yourself, step by step:

1. **Install the server**:
   - `git clone https://github.com/RuMemory/maxgraph && cd maxgraph`
   - Check Docker: `docker info` and `docker compose version` both work → run the installer directly. Docker missing → **ask the user first** (their language): "Docker not found. The installer sets up Rancher Desktop — a free, Docker-compatible engine (on Linux: the official Docker Engine script — adds you to the docker group, root-equivalent rights): ~650 MB, admin rights; Windows may reboot once and continue automatically. Proceed?"
   - Run from the repo root: `bash install.sh --yes` (macOS/Linux) or `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes` (Windows). Exit codes: `0` done · `2` Windows reboot started (resumes at logon — warn the user) · `3` engine declined/impossible → Python fallback: `pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"`, write HOST_FP per `AGENTS.md` step 3a, run `MEMORY_DIR=./data python app/app.py` from the repo root — and tell the user it has **no auto-updates, no autostart after reboot, and listens on 0.0.0.0** (LAN-visible, token required) · `1` error → read `install.log`.
2. **Verify it's alive**: `curl http://localhost:8787/healthz` → `ok`.
3. **Get the access token** (auto-generated on first launch): `cat data/local_token.txt` — or `docker compose logs maxgraph | grep "автогенерация"`.
4. **Hand the token to the user**: ask them to open plugin settings (`/plugin` → maxgraph → configure) and paste it as `api_token`. The default URL `http://localhost:8787/mcp` needs no change.
5. **Verify memory works**: save a test fact with `create_entities`, read it back with `search_nodes`, then tell the user memory is live.

Docs: https://github.com/RuMemory/maxgraph/blob/master/docs/QUICKSTART.md
