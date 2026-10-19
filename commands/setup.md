---
description: Install the MaxGraph memory server and connect this agent to it
---

Set up MaxGraph persistent memory for the user. Do it yourself, step by step:

1. **Install the server**:
   - `git clone https://github.com/RuMemory/maxgraph && cd maxgraph`
   - Run from the repo root: `bash install.sh --yes --project-dir "<user's project root — your original cwd>"` (macOS/Linux) or `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes -ProjectDir "<user's project root>"` (Windows). The installer downloads the native MaxGraph program from the official store (sha256 from the store manifest + ed25519 signature verified), installs it into the user folder (no admin rights, no reboots), writes `hostfp.json` (host fingerprint, hashes only), injects the memory-rules block into the project's `AGENTS.md` (marker-delimited, idempotent) and ends with a **next-steps package** (token + ready config + machine-readable `MAXGRAPH_NEXT_STEPS={...}`). Exit codes: `0` done · `1` error → read `install-native.log`. Re-run = update to the latest version.
   - Python fallback (only when the native binary cannot run, e.g. unsupported OS/arch): `pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"`, write HOST_FP per `AGENTS.md` step 1a, run `MEMORY_DIR=./data python app/app.py` from the repo root — and tell the user it has **no auto-updates, no autostart after reboot, and listens on 0.0.0.0** (LAN-visible, token required).
2. **Verify it's alive**: `curl http://localhost:8787/healthz` → `ok`.
3. **Get the access token**: from the installer's next-steps package — or `data/local_token.txt` inside the install dir (`%LOCALAPPDATA%\MaxGraph` on Windows, `~/.local/share/maxgraph` on Linux, `~/Applications/maxgraph` on macOS).
4. **Hand the token to the user**: ask them to open plugin settings (`/plugin` → maxgraph → configure) and paste it as `api_token`. The default URL `http://localhost:8787/mcp` needs no change.
5. **Verify memory works**: save a test fact with `create_entities`, read it back with `search_nodes`, then tell the user memory is live.

Docs: https://raw.githubusercontent.com/RuMemory/maxgraph/master/AGENTS.md
