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
import json, os, subprocess, sys, urllib.request
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
# NOTE: Codex does NOT expose a reliable live subscription-usage figure. The
# rate_limits in the session logs are per-session snapshots piggybacked on
# inference responses — they go stale the moment a session ends, the windows
# (5h/weekly) don't match what the CLI shows (monthly on some plans), and the
# freshest record is often null. Presenting them as "current" would be lying,
# so we deliberately DON'T. Only cumulative token totals (which are accurate)
# are reported. Live Codex usage lives at chatgpt.com/codex/settings/usage.

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

def claude_sub_usage():
    token = None
    try:  # macOS keychain first, then credentials file
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            token = json.loads(out.stdout)["claudeAiOauth"]["accessToken"]
    except Exception:
        pass
    if not token:
        try:
            creds = json.loads((HOME / ".claude" / ".credentials.json").read_text())
            token = creds["claudeAiOauth"]["accessToken"]
        except Exception:
            return None, None
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": f"Bearer {token}",
                 "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        five = wk = None
        for w in [d.get("five_hour"), *(d.get("rate_limits") or [])]:
            if not isinstance(w, dict):
                continue
            u = w.get("utilization")
            wt = w.get("window") or w.get("name") or ""
            if u is None:
                continue
            if "five" in str(wt) or w is d.get("five_hour"):
                five = u
            elif "seven" in str(wt) or "week" in str(wt):
                wk = max(wk or 0, u)
        if five is None and isinstance(d.get("five_hour"), dict):
            five = d["five_hour"].get("utilization")
        if wk is None and isinstance(d.get("seven_day"), dict):
            wk = d["seven_day"].get("utilization")
        return five, wk
    except Exception:
        return None, None

# --- main -------------------------------------------------------------------

def main():
    try:
        cache = json.loads(CACHE.read_text())
    except Exception:
        cache = {}

    agents = []
    cc = claude_code_totals(cache)
    if cc:
        agents.append(cc)
    codex_agent = codex_totals()
    if codex_agent:
        agents.append(codex_agent)
    try:
        extra = json.loads((CONF / "extra.json").read_text())
        agents += [a for a in extra if isinstance(a, dict)][:4]
    except Exception:
        pass

    # Only Claude exposes a reliable live usage endpoint. Codex intentionally
    # omitted — see codex_totals() note. codex_* kept as null for schema stability.
    claude_5h, claude_wk = claude_sub_usage()
    payload = {
        "sub": {"claude_5h": claude_5h, "claude_wk": claude_wk,
                "codex_5h": None, "codex_wk": None},
        "agents": agents,
    }

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
                import time; time.sleep(65)
                continue
            raise

if __name__ == "__main__":
    main()
