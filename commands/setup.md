---
description: Install the MaxGraph memory server and connect this agent to it
---

Set up MaxGraph persistent memory for the user. Do it yourself, step by step:

1. **Install the server**:
   - `git clone https://github.com/RuMemory/maxgraph && cd maxgraph`
   - Check Docker: `docker info` and `docker compose version` both work → run the installer directly. Docker missing → **ask the user first** (their language): "Docker not found. The installer downloads and runs the official Docker Desktop installer from docker.com (on Linux: the official Docker Engine script — adds you to the docker group, root-equivalent rights): ~600 MB; Docker's installer requests admin rights itself; Windows may reboot once and continue automatically. Proceed?" A declined offer is repeated once with Docker Desktop as the recommended option.
   - Run from the repo root: `bash install.sh --yes --project-dir "<user's project root — your original cwd>"` (macOS/Linux) or `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes -ProjectDir "<user's project root>"` (Windows). The installer writes `hostfp.json` (host fingerprint, hashes only), injects the memory-rules block into the project's `AGENTS.md` (marker-delimited, idempotent) and ends with a **next-steps package** (token + ready config + machine-readable `MAXGRAPH_NEXT_STEPS={...}`). Exit codes: `0` done · `2` Docker Desktop asks for a Windows reboot or has a dialog waiting — resumes automatically at logon (warn the user; otherwise the user finishes the Docker dialog and re-runs install.ps1) · `3` engine declined/impossible → Python fallback: `pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"`, write HOST_FP per `AGENTS.md` step 1a, run `MEMORY_DIR=./data python app/app.py` from the repo root — and tell the user it has **no auto-updates, no autostart after reboot, and listens on 0.0.0.0** (LAN-visible, token required) · `1` error → read `install.log`.
2. **Verify it's alive**: `curl http://localhost:8787/healthz` → `ok`.
3. **Get the access token**: from the installer's next-steps package — or `cat data/local_token.txt` / `docker compose logs maxgraph | grep "автогенерация"`.
4. **Hand the token to the user**: ask them to open plugin settings (`/plugin` → maxgraph → configure) and paste it as `api_token`. The default URL `http://localhost:8787/mcp` needs no change.
5. **Verify memory works**: save a test fact with `create_entities`, read it back with `search_nodes`, then tell the user memory is live.

Docs: https://github.com/RuMemory/maxgraph/blob/master/AGENTS.md
