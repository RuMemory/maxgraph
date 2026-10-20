#!/usr/bin/env python3


































import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MARK = "maxgraph_hook.py"
TIMEOUT = 10


def _home(p: str) -> Path:
    return Path(p).expanduser()


def _ps_str(s: str) -> str:


    return s.replace("`", "``").replace("$", "`$").replace('"', '""')


def _command(host: str) -> str:









    hook = _home("~/.maxgraph/maxgraph_hook.py")
    if os.name == "nt":
        if host == "dsh":
            con = Path(sys.executable)
            if con.name.lower() == "pythonw.exe":
                cand = con.with_name("python.exe")
                con = cand if cand.exists() else con
            return f'& "{_ps_str(str(con))}" "{_ps_str(str(hook))}" --host {host}'
        pyw = Path(sys.executable).with_name("pythonw.exe")
        py = str(pyw if pyw.exists() else Path(sys.executable))
        return f'"{py}" "{hook}" --host {host}'
    return f"python3 ~/.maxgraph/maxgraph_hook.py --host {host}"


def _toml_str(s: str) -> str:



    if "'" not in s and "\n" not in s:
        return "'" + s + "'"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_str(s: str) -> str:



    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'




_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]+$")




def _entry_settings(host: str, event: str) -> dict:

    e = {"hooks": [{"type": "command", "command": _command(host), "timeout": TIMEOUT}]}
    if host == "claude" and event == "PostToolUse":
        e["matcher"] = "*"
    if host == "claude" and event == "PreToolUse":


        e["matcher"] = "mcp__.*maxgraph.*"
        e["hooks"][0]["timeout"] = 5
    if host == "codex" and event == "SessionStart":
        e["matcher"] = "startup"
    return e


def _entry_flat(host: str, _event: str) -> dict:

    return {"command": _command(host)}


def _entry_named(host: str, _event: str) -> list:

    return [{"type": "command", "command": _command(host), "timeout": TIMEOUT}]


HOSTS = {
    "claude": {
        "detect": ["~/.claude"], "config": "~/.claude/settings.json",
        "kind": "settings", "entry": _entry_settings,
        "events": ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
                   "SessionEnd", "PreToolUse"],
    },
    "gemini": {
        "detect": ["~/.gemini/settings.json"], "config": "~/.gemini/settings.json",
        "kind": "settings", "entry": _entry_settings,
        "events": ["SessionStart", "BeforeAgent", "AfterTool", "AfterAgent", "SessionEnd"],
    },
    "codex": {
        "detect": ["~/.codex"], "config": "~/.codex/hooks.json",
        "kind": "settings", "entry": _entry_settings,
        "events": ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"],
        "note": "codex-trust",
    },
    "cursor": {
        "detect": ["~/.cursor"], "config": "~/.cursor/hooks.json",
        "kind": "flat", "entry": _entry_flat,
        "events": ["sessionStart", "beforeSubmitPrompt", "postToolUse", "stop", "sessionEnd"],
        "root": {"version": 1},
    },
    "windsurf": {
        "detect": ["~/.codeium/windsurf"], "config": "~/.codeium/windsurf/hooks.json",
        "kind": "flat", "entry": _entry_flat,
        "events": ["pre_user_prompt", "post_cascade_response"],
    },
    "antigravity": {
        "detect": ["~/.gemini/config", "~/.gemini/antigravity-cli", "~/.gemini/antigravity"],
        "config": "~/.gemini/config/hooks.json",
        "kind": "named", "entry": _entry_named,
        "events": ["SessionStart", "PostToolUse", "Stop"],
    },
    "kimi": {
        "detect": ["~/.kimi-code"], "config": "~/.kimi-code/config.toml",
        "kind": "toml",
        "events": ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"],
    },
    "dsh": {
        "detect": ["~/.dsh/profiles"], "config": None,
        "kind": "dsh",
        "events": ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"],
    },
}




def _real(path: Path) -> Path:

    return Path(os.path.realpath(path))


def _read_cfg(path: Path) -> str:





    return path.read_text(encoding="utf-8-sig")


def _load_json(path: Path):

    path = _real(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(_read_cfg(path))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _backup_once(path: Path) -> None:
    path = _real(path)
    bak = path.with_name(path.name + ".bak-maxgraph")
    if path.exists() and not bak.exists():
        try:
            bak.write_bytes(path.read_bytes())
            shutil.copymode(path, bak)
        except OSError:
            pass


def _atomic_write(path: Path, text: str) -> None:



    path = _real(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_once(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        if path.exists():
            shutil.copymode(path, tmp)
    except OSError:
        pass
    os.replace(tmp, path)


def _write_json(path: Path, data: dict, dry: bool) -> None:
    if dry:
        return
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _marked(text: str) -> bool:


    return any(MARK in ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _restore_text(path: Path, text: str) -> None:



    try:
        _atomic_write(path, text)
    except OSError:
        pass




def _hooks_map(data: dict):


    h = data.get("hooks")
    if h is None:
        h = {}
        data["hooks"] = h
        return h
    return h if isinstance(h, dict) else None


def _apply_settings(cfg_path: Path, spec: dict, remove: bool, dry: bool) -> str:
    data = _load_json(cfg_path)
    if data is None:
        return "skip (config is not valid JSON — left untouched)"
    hooks = _hooks_map(data)
    if hooks is None:
        return "skip ('hooks' key is not a map — left untouched)"
    changed = False
    for ev in spec["events"]:
        lst = hooks.setdefault(ev, [])
        if not isinstance(lst, list):
            continue
        if remove:
            new = [x for x in lst if MARK not in json.dumps(x, ensure_ascii=False)]
            if len(new) != len(lst):
                changed = True
            if new:
                hooks[ev] = new
            else:
                hooks.pop(ev, None)
        else:
            if any(MARK in json.dumps(x, ensure_ascii=False) for x in lst):
                continue
            lst.append(spec["entry"](spec["_host"], ev))
            changed = True
    if not changed:
        return "already registered" if not remove else "not registered"
    if remove and not hooks:
        data.pop("hooks", None)
    _write_json(cfg_path, data, dry)
    return ("removed from " if remove else "registered in ") + str(cfg_path)


def _apply_flat(cfg_path: Path, spec: dict, remove: bool, dry: bool) -> str:
    data = _load_json(cfg_path)
    if data is None:
        return "skip (config is not valid JSON — left untouched)"
    for k, v in spec.get("root", {}).items():
        data.setdefault(k, v)
    hooks = _hooks_map(data)
    if hooks is None:
        return "skip ('hooks' key is not a map — left untouched)"
    changed = False
    for ev in spec["events"]:
        lst = hooks.setdefault(ev, [])
        if not isinstance(lst, list):
            continue
        if remove:
            new = [x for x in lst
                   if not (isinstance(x, dict) and MARK in str(x.get("command", "")))]
            changed = changed or len(new) != len(lst)
            if new:
                hooks[ev] = new
            else:
                hooks.pop(ev, None)
        else:
            if any(isinstance(x, dict) and MARK in str(x.get("command", "")) for x in lst):
                continue
            lst.append(spec["entry"](spec["_host"], ev))
            changed = True
    if not changed:
        return "already registered" if not remove else "not registered"
    if remove and not hooks:
        data.pop("hooks", None)
    _write_json(cfg_path, data, dry)
    return ("removed from " if remove else "registered in ") + str(cfg_path)


def _apply_named(cfg_path: Path, spec: dict, remove: bool, dry: bool) -> str:
    data = _load_json(cfg_path)
    if data is None:
        return "skip (config is not valid JSON — left untouched)"
    if remove:

        ours = data.get("maxgraph")
        if not ours or MARK not in json.dumps(ours, ensure_ascii=False):
            return "not registered"
        data.pop("maxgraph", None)
        _write_json(cfg_path, data, dry)
        return "removed from " + str(cfg_path)
    if "maxgraph" in data:
        return "already registered"
    data["maxgraph"] = {ev: spec["entry"](spec["_host"], ev) for ev in spec["events"]}
    _write_json(cfg_path, data, dry)
    return "registered in " + str(cfg_path)


def _toml_blocks(text: str) -> list:


    lines = text.splitlines(keepends=True)
    blocks, cur = [], []
    for ln in lines:
        if ln.lstrip().startswith("[") and cur:
            blocks.append("".join(cur))
            cur = []
        cur.append(ln)
    if cur:
        blocks.append("".join(cur))
    return blocks


def _apply_toml(cfg_path: Path, spec: dict, remove: bool, dry: bool) -> str:
    cfg_path = _real(cfg_path)
    text = _read_cfg(cfg_path) if cfg_path.exists() else ""
    if remove:
        if not _marked(text):
            return "not registered"
        kept = [b for b in _toml_blocks(text)
                if not (b.lstrip().startswith("[[hooks]]") and _marked(b))]
        out = "".join(kept)
        if not dry:
            _atomic_write(cfg_path, out)
        return "removed from " + str(cfg_path)
    if _marked(text):
        return "already registered"
    add = "".join(f'\n[[hooks]]\nevent = "{ev}"\ncommand = {_toml_str(_command(spec["_host"]))}\n'
                  for ev in spec["events"])
    if not dry:
        _atomic_write(cfg_path, text.rstrip("\n") + "\n" + add)
    return "registered in " + str(cfg_path)


_DSH_BLOCK = """- insert:
    - id: hooks-maxgraph
      name: "@deepseek-ai/dsh-hooks-claude-code"
      config:
        configPath: {config_path}
        projectDir: {project_dir}
"""

_DSH_MCP_BLOCK = """- insert:
    - id: mcp-maxgraph
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: maxgraph
        transport: streamable-http
        url: {url}
        headers:
          Authorization: {auth}
"""




_MCP_ID_RE = re.compile(r"^\s*-?\s*id:\s*[\"']?(mcp-maxgraph|mcp-remotemem)[\"']?\s*(#.*)?$",
                        re.M)
_MCP_SERVER_RE = re.compile(r"^\s*serverName:\s*[\"']?(maxgraph|remotemem)[\"']?\s*(#.*)?$",
                            re.M)
_HOOKS_ID_RE = re.compile(r"^\s*-?\s*id:\s*[\"']?hooks-maxgraph[\"']?\s*(#.*)?$", re.M)
_BLOCK_SCALAR_RE = re.compile(r":\s*[|>][+-]?\d?\s*(#.*)?$")


def _no_comments(text: str) -> str:

    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _strip_block_scalars(lines: list) -> list:


    out = []
    scalar_indent = None
    for ln in lines:
        stripped = ln.strip()
        ind = len(ln) - len(ln.lstrip(" "))
        if scalar_indent is not None:
            if not stripped or ind > scalar_indent:
                continue
            scalar_indent = None
        out.append(ln)
        if _BLOCK_SCALAR_RE.search(ln):
            scalar_indent = ind
    return out


def _is_our_mcp_row(lines: list) -> bool:



    t = _no_comments("\n".join(_strip_block_scalars(lines)))
    return "dsh-mcp-client" in t and (bool(_MCP_ID_RE.search(t))
                                    or bool(_MCP_SERVER_RE.search(t)))


def _is_our_hooks_row(lines: list) -> bool:

    return bool(_HOOKS_ID_RE.search(_no_comments("\n".join(_strip_block_scalars(lines)))))











_DSH_HOOKS_PKG = "@deepseek-ai/dsh-hooks-claude-code"






_DSH_HOOKS_PEERS = ("@deepseek-ai/dsh-hook-protocol", "@deepseek-ai/dsh-llm")



_DSH_PKG_NAME_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")


_DSH_RANGE_RE = re.compile(r"^[~^]?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")



_DSH_REPAIR_LIMIT = 8






_DSH_VER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")







_DSH_PRIMARY_RUNTIME = "~/.dsh/dsh-runtimes/dsh-primary-runtime"


def _dsh_runtime() -> tuple:



    rt = _home(_DSH_PRIMARY_RUNTIME) / "runtime.json"
    try:
        ver = json.loads(_read_cfg(rt)).get("desktopVersion") or ""
    except Exception:
        return ("", "", "")
    if not (isinstance(ver, str) and _DSH_VER_RE.fullmatch(ver)):
        return ("", "", "")
    base = rt.parent / "dependencies"
    node = base / "node" / "bin" / ("node.exe" if os.name == "nt" else "node")
    pnpm = base / "pnpm" / "bin" / "pnpm.cjs"
    if not (node.exists() and pnpm.exists()):
        return ("", "", "")
    return (ver, str(node), str(pnpm))


def _dsh_pkg_version(profile_dir: Path, pkg: str) -> str:



    pj = profile_dir / "node_modules" / pkg / "package.json"
    try:
        v = json.loads(_read_cfg(pj)).get("version")
        return v if isinstance(v, str) else ""
    except Exception:
        return ""


def _sanitize_err(line: str) -> str:



    return re.sub(r"://[^@/\s]+@", "://", line)[:200]


def _dsh_bridge_entry(profile_dir: Path) -> Path:

    return profile_dir / "node_modules" / _DSH_HOOKS_PKG / "lib" / "index.js"


def _dsh_pnpm_add(node: str, pnpm: str, profile_dir: Path, specs: list) -> tuple:



    try:
        r = subprocess.run([node, pnpm, "add", "--save-exact",
                            "--config.auto-install-peers=false",
                            "--ignore-scripts",
                            "--registry=https://registry.npmjs.org", *specs],
                           cwd=str(profile_dir), capture_output=True, timeout=300)
    except Exception as ex:
        return (False, f" {type(ex).__name__}")
    if r.returncode == 0:
        return (True, "")
    err = r.stderr.decode("utf-8", "replace").strip().splitlines()
    return (False, f" exit {r.returncode}"
                   + (": " + _sanitize_err(err[-1]) if err else ""))


def _dsh_import_check(node: str, entry: Path) -> tuple:









    if not entry.exists():
        return (False, "", "нет " + str(entry))
    script = ("import(" + json.dumps(entry.resolve().as_uri()) + ")"
              ".then(()=>console.log('IMPORT OK'))"
              ".catch(e=>{console.error('IMPORT FAIL: '+e.message);process.exit(3)})")
    try:
        r = subprocess.run([node, "--input-type=module", "-e", script],
                           capture_output=True, timeout=60)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
    except Exception as ex:
        return (False, "", type(ex).__name__)
    if "IMPORT OK" in out:
        return (True, "", "")
    m = re.search(r"Cannot find package '([^']+)' imported from (.+)", out)
    if m:


        return (False, m.group(1), m.group(2).rstrip("\r").strip())
    tail = out.strip().splitlines()
    return (False, "", _sanitize_err(tail[-1]) if tail else f"exit {r.returncode}")


def _dsh_peer_spec(profile_dir: Path, name: str, importer: str, ver: str) -> str:






    if not _DSH_PKG_NAME_RE.fullmatch(name):
        return ""
    if name.startswith("@deepseek-ai/dsh-"):
        return f"{name}@{ver}"
    try:
        root = (profile_dir / "node_modules").resolve()
        d = Path(importer).resolve().parent
        d.relative_to(root)
    except (ValueError, OSError):
        return ""
    while True:
        pj = d / "package.json"
        if pj.exists():
            try:
                meta = json.loads(_read_cfg(pj))
            except Exception:
                return ""
            for key in ("peerDependencies", "dependencies", "devDependencies"):
                rng = (meta.get(key) or {}).get(name)
                if isinstance(rng, str) and _DSH_RANGE_RE.fullmatch(rng):
                    return f"{name}@{rng}"
            return ""
        if d == root:
            return ""
        parent = d.parent
        if parent == d:
            return ""
        d = parent


def _dsh_ensure_hook_plugin(profile_dir: Path, dry: bool) -> str:















    ver, node, pnpm = _dsh_runtime()
    pkgs = (_DSH_HOOKS_PKG, *_DSH_HOOKS_PEERS)
    specs = [f"{p}@{ver}" if ver else f"{p}@<версия DSH>" for p in pkgs]
    manual = (f'cd "{profile_dir}" && pnpm add --save-exact '
              f"--config.auto-install-peers=false --ignore-scripts "
              f"--registry=https://registry.npmjs.org {' '.join(specs)}")
    if dry:
        return f"would install {' '.join(specs)} into {profile_dir}"
    if not node or not ver:
        return f"hooks plugin NOT installed (DSH не вызовет хук) — установите: {manual}"
    stale = [p for p in pkgs if _dsh_pkg_version(profile_dir, p) != ver]
    was_present = any(_dsh_pkg_version(profile_dir, p) for p in pkgs)
    if stale:
        ok, err = _dsh_pnpm_add(node, pnpm, profile_dir, [f"{p}@{ver}" for p in stale])
        bad = [p for p in stale if _dsh_pkg_version(profile_dir, p) != ver]
        if not ok or bad:
            if not ok:
                why = f" (pnpm{err})"
            else:
                got = ", ".join(_dsh_pkg_version(profile_dir, p) or "нет" for p in stale)
                why = f" (установлено {got} ≠ {ver})"
            return f"hooks plugin install failed{why} — установите вручную: {manual}"

    entry = _dsh_bridge_entry(profile_dir)
    for _ in range(_DSH_REPAIR_LIMIT):
        ok, missing, detail = _dsh_import_check(node, entry)
        if ok:
            if not stale:
                return "hooks plugin present"
            return "hooks plugin re-pinned" if was_present else "hooks plugin installed"
        if not missing:
            return (f"hooks plugin import check failed ({detail}) — "
                    f"проверьте вручную: {manual}")
        spec = _dsh_peer_spec(profile_dir, missing, detail, ver)
        if not spec:
            return (f"hooks plugin peer {missing!r} не доустановлен (небезопасное "
                    f"имя или нет range у импортёра) — установите вручную: {manual}")
        ok, err = _dsh_pnpm_add(node, pnpm, profile_dir, [spec])
        if not ok:
            return (f"hooks plugin peer install failed (pnpm{err}) — "
                    f"установите вручную: {manual}")
        if not _dsh_pkg_version(profile_dir, missing):


            return (f"hooks plugin peer install failed (установленный {missing} "
                    f"не читается) — установите вручную: {manual}")
    return (f"hooks plugin import still failing после {_DSH_REPAIR_LIMIT} "
            f"попыток — установите вручную: {manual}")


def _mcp_token() -> str:




    tok = os.environ.get("MEMORY_TOKEN") or ""
    if not tok.strip():
        try:
            f = Path(__file__).resolve().parent.parent / "data" / "local_token.txt"
            tok = _read_cfg(f)
        except OSError:
            return ""
    return "".join(tok.split())


def _dsh_row_spans(lines: list) -> list:



    items = []
    scalar_indent = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        ind = len(ln) - len(ln.lstrip(" "))
        if scalar_indent is not None:
            if not stripped or ind > scalar_indent:
                continue
            scalar_indent = None
        if _BLOCK_SCALAR_RE.search(ln):
            scalar_indent = ind
        if stripped.startswith("- ") or stripped == "-":
            items.append((i, ind))
    spans = []
    for k, (i, ind) in enumerate(items):
        end = len(lines)
        for j, ind2 in items[k + 1:]:
            if ind2 <= ind:
                end = j
                break
        spans.append((i, end, ind))
    return spans


def _innermost(hits: list) -> list:


    return [h for h in hits
            if not any(o is not h and o[0] >= h[0] and o[1] <= h[1] for o in hits)]


def _dsh_mcp_rows(lines: list) -> list:




    hits = []
    for s, e, ind in _dsh_row_spans(lines):
        if _is_our_mcp_row(lines[s:e]):
            hits.append((s, e, ind))
    out = []
    for s, e, ind in _innermost(hits):
        mounted = False
        cur = ind
        for k in range(s - 1, -1, -1):
            ln = lines[k]
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            li = len(ln) - len(ln.lstrip(" "))
            if li < cur:
                head = ln.split("#", 1)[0].strip()
                if head in ("- insert:", "insert:"):
                    mounted = True
                    break
                cur = li
        out.append((s, e, mounted))
    return out


def _dsh_drop_spans(lines: list, spans: list) -> list:


    for s, e in sorted(spans, reverse=True):
        del lines[s:e]
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "- insert:" and not ln.startswith(" "):
            j = i + 1
            has_child = False
            while j < len(lines):
                nl = lines[j]
                if nl.strip() and not nl.lstrip().startswith("#"):
                    has_child = nl.startswith(" ")
                    break
                j += 1
            if not has_child:
                i += 1
                continue
        out.append(ln)
        i += 1
    return out


def _dsh_top_level_ok(text: str) -> bool:



    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln.startswith(" "):
            continue
        if not ln.startswith("- "):
            return False
    return True


def _apply_dsh(spec: dict, remove: bool, dry: bool) -> str:
    profiles = sorted(glob.glob(str(_home("~/.dsh/profiles/*/cordis.patch.yml"))))
    hooks_json = _home("~/.maxgraph/hooks.json")
    if not profiles:
        return "skip (no ~/.dsh/profiles/*/cordis.patch.yml)"
    if not remove:

        data = _load_json(hooks_json)
        if data is None:
            return "skip (~/.maxgraph/hooks.json is not valid JSON)"
        hooks = _hooks_map(data)
        if hooks is None:
            return "skip (~/.maxgraph/hooks.json 'hooks' is not a map)"
        changed = False
        for ev in spec["events"]:
            lst = hooks.setdefault(ev, [])
            ours = next((x for x in lst
                         if MARK in json.dumps(x, ensure_ascii=False)), None)
            if ours is not None:



                for h in (ours.get("hooks") or []):
                    if isinstance(h, dict) and h.get("type") == "command":
                        want = _command("dsh")
                        if h.get("command") != want:
                            h["command"] = want
                            changed = True
                continue
            lst.append({"matcher": ".*", "hooks": [
                {"type": "command", "command": _command("dsh"), "timeout": TIMEOUT}]})
            changed = True
        if changed:
            _write_json(hooks_json, data, dry)
    token = _mcp_token()
    token_ok = bool(token) and bool(_TOKEN_RE.fullmatch(token))
    results = []
    for prof in profiles:
        try:
            results.append(_dsh_apply_profile(Path(prof), spec, remove, dry,
                                              token, token_ok, hooks_json))
        except Exception as ex:


            results.append(f"{prof}: ERROR {ex}")
    return "; ".join(results)


def _dsh_apply_profile(p: Path, spec: dict, remove: bool, dry: bool,
                       token: str, token_ok: bool, hooks_json: Path) -> str:
    text = _read_cfg(p) if p.exists() else ""
    lines = text.splitlines(keepends=True)
    if remove:



        spans = _dsh_row_spans(lines)
        drop = []
        for s, e, _ind in spans:
            if _is_our_hooks_row(lines[s:e]) or _is_our_mcp_row(lines[s:e]):
                drop.append((s, e))
        drop = _innermost(drop)
        if not drop:
            return f"{p}: not registered"
        if not dry:
            _atomic_write(p, "".join(_dsh_drop_spans(lines, drop)))

            check = _read_cfg(p)
            check_lines = check.splitlines(keepends=True)
            gone = not _is_our_hooks_row(check_lines) and not _dsh_mcp_rows(check_lines)
            if not gone or not _dsh_top_level_ok(check):
                _restore_text(p, text)
                return f"{p}: ERROR post-check failed — reverted"
        return f"{p}: removed"
    status = []
    add_blocks = []

    if _is_our_hooks_row(lines):
        status.append("hooks already registered")
    else:
        add_blocks.append(_DSH_BLOCK.format(
            config_path=_yaml_str(str(hooks_json).replace("\\", "/")),
            project_dir=_yaml_str(str(_home("~")).replace("\\", "/"))))
        status.append("hooks registered")

    mcp_rows = _dsh_mcp_rows(lines)
    broken = [(s, e) for s, e, mounted in mcp_rows if not mounted]
    dropped = False
    if any(mounted for _s, _e, mounted in mcp_rows):
        status.append("mcp already registered")
    elif not token:
        status.append("mcp skipped (no token — data/local_token.txt)")
    elif not token_ok:
        status.append("mcp skipped (bad token charset)")
    else:
        if broken:
            lines = _dsh_drop_spans(lines, _innermost(broken))
            dropped = True
            status.append("mcp repaired (was bare '- id:', DSH skips those)")
        else:
            status.append("mcp registered")
        add_blocks.append(_DSH_MCP_BLOCK.format(
            url=_yaml_str("http://localhost:8787/mcp"),
            auth=_yaml_str("Bearer " + token)))

    status.append(_dsh_ensure_hook_plugin(p.parent, dry))
    if not add_blocks and not dropped:
        return f"{p}: " + ", ".join(status)
    if not _dsh_top_level_ok(text):
        return f"{p}: skip (top level is not a list — left untouched)"
    if not dry:
        new_text = "".join(lines).rstrip("\n") + "\n" + "".join(add_blocks)
        _atomic_write(p, new_text)



        check = _read_cfg(p)
        want = (["hooks-maxgraph"] if any("hooks registered" in s for s in status) else []) \
            + (["mcp-maxgraph"] if any(s.startswith("mcp registered")
                                       or s.startswith("mcp repaired") for s in status) else [])
        rows_after = _dsh_mcp_rows(check.splitlines(keepends=True))


        attempted_repair = bool(broken) and any(s.startswith("mcp repaired") for s in status)
        still_broken = attempted_repair and any(not m for _s, _e, m in rows_after)
        if any(w not in check for w in want) or not _dsh_top_level_ok(check) or still_broken:
            _restore_text(p, text)
            return f"{p}: ERROR post-check failed — reverted"
        if os.name != "nt" and "Bearer" in new_text:
            try:
                os.chmod(_real(p), 0o600)
            except OSError:
                pass
    return f"{p}: " + ", ".join(status)


def _detected(spec: dict) -> bool:
    return any(_home(d).exists() for d in spec["detect"])


def apply_host(host: str, remove: bool = False, dry: bool = False) -> str:
    spec = {**HOSTS[host], "_host": host}
    if not _detected(spec):
        return f"{host}: not found"
    if spec["kind"] == "dsh":
        res = _apply_dsh(spec, remove, dry)
    else:
        fn = {"settings": _apply_settings, "flat": _apply_flat,
              "named": _apply_named, "toml": _apply_toml}[spec["kind"]]
        res = fn(_home(spec["config"]), spec, remove, dry)
    msg = f"{host}: {res}"
    if spec.get("note") == "codex-trust" and not remove and "registered" in res:
        msg += "  [скажи пользователю: в Codex один раз выполнить /hooks и доверить хуки]"
    return msg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MaxGraph capture-hook registrar")
    ap.add_argument("--host", choices=sorted(HOSTS), help="один хост")
    ap.add_argument("--auto", action="store_true", help="все найденные хосты")
    ap.add_argument("--remove", action="store_true", help="снять регистрацию")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not args.auto and not args.host:
        ap.error("нужен --host X или --auto")
    hosts = sorted(HOSTS) if args.auto else [args.host]
    for h in hosts:
        try:
            print(apply_host(h, remove=args.remove, dry=args.dry_run))
        except Exception as ex:
            print(f"{h}: ERROR {ex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
