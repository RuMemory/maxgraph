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

None required upfront. The installer checks for Docker and, with the user's
consent, installs an engine itself (official Docker Desktop from docker.com on
Windows/macOS; official Docker Engine on Linux). A Python 3.12+ fallback
exists for machines where no engine may be installed.

## Step 1 — install

```bash
git clone https://github.com/RuMemory/maxgraph.git
cd maxgraph
```

Check Docker: `docker info` answers without errors and `docker compose version` works.

Run the installer from the repo root — it installs a missing engine, computes
`HOST_FP` (sha256 of the host hardware id; never overwrites a non-empty value; the
raw id never leaves the host), writes `.env` and starts the server (prebuilt GHCR
image, local build as fallback):

- macOS/Linux: `bash install.sh --yes`
- Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Yes`

**Docker missing → ask the user FIRST** (in their language): "Docker not found.
The installer will download and run the official Docker Desktop installer from
docker.com (on Linux: the official Docker Engine script — it also adds your
user to the docker group, permanent root-equivalent rights): ~600 MB download;
Docker's installer requests admin rights itself; on Windows one reboot may be
required — the installation continues automatically afterwards. Proceed?"
Consent → run with `--yes`/`-Yes` as above. A declined offer is repeated once
with Docker Desktop as the recommended option; a second refusal → Python
fallback. During setup a Docker Desktop installer window appears on screen —
the user clicks through it (admin prompt; sign-in can be skipped).
Docker is the only recommended path — NEVER offer Python as an equal option
or mark it as recommended; Python is only the fallback after refusal(s).

Exit codes: `0` — MaxGraph is running, continue at step 2; `2` — Docker Desktop
asks for a Windows reboot or has a dialog waiting: the installer resumes
automatically at logon (warn the user); if auto-resume did not fire, the user
finishes the Docker Desktop dialogs and re-runs `install.ps1`; `3` — no
engine → Python fallback below; `1` — error → read `install.log`.

### Python fallback (only when Docker is declined or impossible)

```bash
pip install "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"   # mcp<2 is mandatory: FastMCP was renamed in 2.x
```

Write `HOST_FP` (64 hex) into `.env` (create it from `.env.example` if missing;
never overwrite a non-empty value; the raw hardware id never leaves the host):

- Windows (PowerShell): `$guid=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid; [BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes("$guid"))).Replace('-','').ToLower()`
- macOS: `printf %s "$(ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/{print $4}')" | shasum -a 256 | awk '{print $1}'`
- Linux: `tr -d '[:space:]' < /etc/machine-id | sha256sum | awk '{print $1}'`

Run from the repo root, always with `MEMORY_DIR=./data` (otherwise data lands in
`/data` and won't be picked up by a later switch to Docker):

```powershell
$env:MEMORY_DIR="./data"; python app/app.py    # Windows
```

```bash
MEMORY_DIR=./data python app/app.py            # macOS/Linux
```

**You MUST tell the user what Python mode loses:** no auto-updates (manual only:
`git pull` + `pip install -U …`), no autostart (the server must be restarted
after every reboot), no network isolation (it listens on 0.0.0.0 — reachable from
the LAN, token required). Switch to Docker later: install Docker and re-run the
install script from the install folder — `./data` is picked up.

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

## What the installer leaves behind

Besides Docker containers and `data/`: `install.log` / `install-state.json` in the
repo folder, and — only while a reboot is pending — a `MaxGraphInstall` entry in
the user's `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce` (removed
automatically on success). The uninstaller (next section) removes them.
Installations made by MaxGraph 0.39–0.41 may additionally carry a Rancher Desktop
defaults profile at `HKLM\SOFTWARE\Policies\Rancher Desktop\Defaults` (Windows) or
`~/Library/Preferences/io.rancherdesktop.profile.defaults.plist` (macOS) — the
uninstaller removes that too.

## Uninstall

When the user asks to remove MaxGraph — full cleanup, no leftovers:

1. **Run the uninstaller** from the install folder — it removes the containers and
   images (`maxgraph`, `maxgraph-updater`), the legacy Rancher defaults profile,
   the Windows RunOnce resume key (HKCU; HKLM on legacy installs) and
   `~/.maxgraph` (session-hook spool):
   - macOS/Linux: `bash uninstall.sh`
   - Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1`

   Flags: `--dry-run` shows what would be removed without touching anything;
   `--yes` answers the prompts for the user (you already asked them in chat);
   `--wipe-data` also deletes `./data` and the updater's backup volume.
   **Memory data is kept by default — ask the user FIRST:** the knowledge graph,
   license and tokens live in `./data`; deletion is irreversible. Wipe only after
   an explicit "yes" (or pass `--wipe-data` when the user already said it).
   Tell the user: the trial is anchored to this machine (hardware fingerprint) —
   wiping data does NOT restart it; a paid license is restored by email.
2. **Remove the MCP entry** (`maxgraph`) from every client config you touched in
   step 4 (Claude plugin users: `claude plugin remove maxgraph@maxgraph`).
3. **Remove the rules block** between `<!-- maxgraph:start -->` and
   `<!-- maxgraph:end -->` from the project's `AGENTS.md`. In the agent's GLOBAL
   rules file: show the user any maxgraph lines you find and remove only
   install-made blocks after their explicit "yes".
4. **Data kept (default)?** Move `./data` out of the install folder BEFORE
   deleting it (or delete everything except `data/`) — the folder removal must
   not take the memory with it. Deleting `data/` is allowed only after the
   user's explicit wipe decision. Then confirm with the user and **delete the
   install folder**.

Install folder already deleted? Then: `docker rm -f maxgraph maxgraph-updater`
(plus `docker volume ls -q | grep -E '(^|_)updater-state$' | xargs -r docker volume rm`
and `docker rmi` of the built images when the data must go too), then steps 2–4
by hand. Docker Desktop / Rancher themselves stay installed — other apps may use
them.
Leftovers the uninstaller deliberately leaves: on Linux the user stays in the
`docker` group (root-equivalent; remove it by hand if MaxGraph was the only
reason: `sudo gpasswd -d <user> docker`), on Windows the WSL2 features stay
enabled.
