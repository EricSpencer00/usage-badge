#!/usr/bin/env python3
"""usage-badge collector — stdlib only, easy to audit.

Reads local agent logs (read-only), computes aggregate token/cost numbers,
and POSTs a ~1 KB JSON blob to your Cloudflare Worker. That blob is the ONLY
data that leaves this machine. Network activity: one optional GET to
api.anthropic.com (subscription usage %) and one POST to your own worker.

Setup:
  mkdir -p ~/.config/usage-badge
  echo 'https://usage-badge.<your-acct>.workers.dev/ingest' > ~/.config/usage-badge/url
  echo '<your INGEST_TOKEN>' > ~/.config/usage-badge/token
  chmod 600 ~/.config/usage-badge/token
Optional extra agents (ollama, claude api, ...): ~/.config/usage-badge/extra.json
  [{"label": "ollama cloud", "tokens": 12345678, "cost_usd": 0}]
"""
import json, os, shutil, subprocess, sys, time, urllib.request
from pathlib import Path

HOME = Path.home()
CONF = HOME / ".config" / "usage-badge"
CACHE = CONF / "cache.json"  # per-file parse cache: path -> [size, mtime, totals]

# Pricing per million tokens (input, output, cache_write, cache_read).
# Estimates from public pricing pages; last checked 2026-07-14.
PRICING = {
    "claude-opus": (15, 75, 18.75, 1.5),
    "claude-sonnet": (3, 15, 3.75, 0.30),
    "claude-haiku": (1, 5, 1.25, 0.10),
    "claude-fable": (15, 75, 18.75, 1.5),
    "gpt-5": (1.25, 10, 0, 0.125),  # codex default, rough
    "_default": (3, 15, 3.75, 0.30),
}

def price_for(model):
    m = (model or "").lower()
    for k, v in PRICING.items():
        if k != "_default" and k in m:
            return v
    return PRICING["_default"]

# --- Claude Code: sum message.usage across project session logs -------------

def claude_code_totals(cache):
    root = HOME / ".claude" / "projects"
    tokens = cost = 0.0
    seen = set()
    if not root.is_dir():
        return None
    for f in root.glob("*/*.jsonl"):
        st = f.stat()
        key = str(f)
        c = cache.get(key)
        if c and c[0] == st.st_size and c[1] == st.st_mtime:
            tokens += c[2]; cost += c[3]
            continue
        ft = fc = 0.0
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    if not isinstance(u, dict) or "output_tokens" not in u:
                        continue
                    mid = (msg.get("id"), d.get("requestId"))
                    if mid[0] and mid in seen:
                        continue
                    seen.add(mid)
                    i, o = u.get("input_tokens", 0), u.get("output_tokens", 0)
                    cw = u.get("cache_creation_input_tokens", 0)
                    cr = u.get("cache_read_input_tokens", 0)
                    pi, po, pcw, pcr = price_for(msg.get("model"))
                    ft += i + o + cw + cr
                    fc += (i * pi + o * po + cw * pcw + cr * pcr) / 1e6
        except OSError:
            continue
        cache[key] = [st.st_size, st.st_mtime, ft, fc]
        tokens += ft; cost += fc
    return {"label": "claude code", "tokens": int(tokens), "cost_usd": round(cost, 2)}

# --- Codex: cumulative token totals from session logs -----------------------
#
# Token totals come from logs (accurate). Codex's *live usage %* is fetched
# separately via CodexBar's OAuth source (see codex_usage_bars) rather than
# scraped from the stale per-session rate_limit snapshots in these logs.

def codex_totals():
    root = HOME / ".codex" / "sessions"
    if not root.is_dir():
        return None
    tokens = 0
    for f in root.rglob("*.jsonl"):
        last = None
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    if '"token_count"' in line:
                        last = line
        except OSError:
            continue
        if not last:
            continue
        try:
            d = json.loads(last)
            tokens += d["payload"]["info"]["total_token_usage"]["total_tokens"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
    pi, _, _, _ = price_for("gpt-5")
    return {"label": "codex", "tokens": int(tokens),
            "cost_usd": round(tokens * pi / 1e6, 2)} if tokens else None

# --- Claude subscription usage % via local OAuth credential ------------------
#
# Uses the Claude Code login you already have. The access token lives in
# ~/.claude/.credentials.json and expires every ~8h; when it's expired we
# refresh it with the stored refresh token (the same flow Claude Code uses) and
# write the rotated tokens back to that same file so Claude Code stays in sync.
# Endpoints/client id are Claude Code's own. Nothing here runs a model — the
# usage endpoint is a read-only reporting call that costs zero tokens.

CLAUDE_CRED = HOME / ".claude" / ".credentials.json"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
UA = "usage-badge-collector/1.0 (claude-cli-compatible)"

def _valid_access_token():
    """Return a currently-valid Claude access token, refreshing if needed."""
    try:
        creds = json.loads(CLAUDE_CRED.read_text())
        o = creds["claudeAiOauth"]
    except Exception:
        return None
    if o.get("expiresAt", 0) > time.time() * 1000 + 60_000:
        return o["accessToken"]  # still valid
    refresh = o.get("refreshToken")
    if not refresh:
        return None  # can't refresh; caller degrades gracefully
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": refresh,
                       "client_id": CLAUDE_CLIENT_ID}).encode()
    req = urllib.request.Request(CLAUDE_TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
    except Exception:
        return None
    o["accessToken"] = tok["access_token"]
    if tok.get("refresh_token"):
        o["refreshToken"] = tok["refresh_token"]
    o["expiresAt"] = int(time.time() * 1000) + int(tok.get("expires_in", 28800)) * 1000
    tmp = CLAUDE_CRED.with_suffix(".usage-badge.tmp")  # atomic write-back, 0600
    tmp.write_text(json.dumps(creds))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CLAUDE_CRED)
    return o["accessToken"]

def claude_sub_usage():
    token = _valid_access_token()
    if not token:
        return None, None
    req = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={"Authorization": f"Bearer {token}",
                 "anthropic-beta": "oauth-2025-04-20", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        five = (d.get("five_hour") or {}).get("utilization")
        wk = (d.get("seven_day") or {}).get("utilization")
        return five, wk
    except Exception:
        return None, None

# --- CodexBar: live Codex usage + accurate multi-provider cost --------------
#
# CodexBar (github.com/steipete/codexbar, `brew install codexbar`) is a
# maintained tool that fetches Codex usage via the official OAuth token and
# computes per-provider token/cost from local logs with correct per-model
# pricing. We shell out to it when present and degrade to stdlib parsing when
# not — so forks without CodexBar still work.

def _codexbar():
    return shutil.which("codexbar")

def _run_codexbar(args, timeout=30):
    try:
        out = subprocess.run([_codexbar(), *args], capture_output=True,
                             text=True, timeout=timeout)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None

_WINDOW_LABEL = {300: "5h", 1440: "day", 10080: "wk", 43200: "mo"}

def codex_usage_bars():
    """Live Codex subscription bars via CodexBar's OAuth source, or []."""
    if not _codexbar():
        return []
    data = _run_codexbar(["usage", "--provider", "codex", "--source", "oauth",
                          "--format", "json"])
    if not isinstance(data, list):
        return []
    bars = []
    for entry in data:
        u = (entry or {}).get("usage") or {}
        for slot in ("primary", "secondary"):
            w = u.get(slot)
            if not isinstance(w, dict) or w.get("usedPercent") is None:
                continue
            lbl = _WINDOW_LABEL.get(w.get("windowMinutes"), slot)
            bars.append({"label": f"codex {lbl}", "pct": w["usedPercent"]})
    return bars

# --- Ollama: read the local metering proxy's tally --------------------------

def ollama_agent():
    try:
        t = json.loads((CONF / "ollama-tally.json").read_text())
    except Exception:
        return None
    if not t.get("tokens"):
        return None
    return {"label": "ollama", "tokens": int(t["tokens"]),
            "cost_usd": round(float(t.get("cost_usd") or 0), 2)}

# --- main -------------------------------------------------------------------

def main():
    try:
        cache = json.loads(CACHE.read_text())
    except Exception:
        cache = {}

    # Per-provider token/cost from local logs (both providers, with pricing).
    # CodexBar's cost output is provider-incomplete and priceless on some setups,
    # so the stdlib parser is the primary source here; CodexBar is used only for
    # live Codex usage % below (the one thing it does that logs can't).
    agents = []
    for fn in (lambda: claude_code_totals(cache), codex_totals):
        a = fn()
        if a:
            agents.append(a)
    oa = ollama_agent()
    if oa:
        agents.append(oa)
    try:  # user-supplied extras (other providers with no readable source)
        extra = json.loads((CONF / "extra.json").read_text())
        agents += [a for a in extra if isinstance(a, dict)][:4]
    except Exception:
        pass

    # Subscription bars: Claude via OAuth (live), Codex via CodexBar (live).
    # Labels are dynamic so plan/window changes (5h/weekly/monthly) never break.
    sub = []
    claude_5h, claude_wk = claude_sub_usage()
    if claude_5h is not None:
        sub.append({"label": "claude 5h", "pct": claude_5h})
    if claude_wk is not None:
        sub.append({"label": "claude wk", "pct": claude_wk})
    sub += codex_usage_bars()

    payload = {"sub": sub, "agents": agents[:6]}

    CONF.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache))

    if "--dry-run" in sys.argv:
        print(json.dumps(payload, indent=2))
        return

    url = (CONF / "url").read_text().strip()
    tok = (CONF / "token").read_text().strip()
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "usage-badge-collector/1.0"})
    for attempt in (1, 2):  # one retry if the worker's 60s write gap hits
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                print(r.status, r.read().decode())
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                time.sleep(65)
                continue
            raise

if __name__ == "__main__":
    main()
