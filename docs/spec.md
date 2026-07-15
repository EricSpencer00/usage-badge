# usage-badge — design spec (2026-07-14)

A toy, public-source README badge showing AI agent usage: Claude Code and Codex
subscription window usage (5-hour / weekly %), plus estimated tokens and cost
across agents. Hosted on Cloudflare Workers (free tier), data pushed from your
own machine. Nothing runs on your machine that you didn't put there; nothing
leaves your machine except a ~1 KB JSON blob of aggregate numbers.

## Architecture (push model)

```
your machine                      cloudflare                  github
┌─────────────────┐   HTTPS POST  ┌──────────┐   GET /badge.svg  ┌────────┐
│ collector.py    │ ────────────▶ │  worker  │ ◀──────────────── │ camo / │
│ (launchd/cron)  │  bearer token │  + KV    │    (public SVG)   │ README │
└─────────────────┘               └──────────┘                   └────────┘
```

- **collector/collect.py** — stdlib-only Python. Reads local agent logs,
  computes aggregates, POSTs to the worker. Runs every 30 min via launchd/cron.
- **worker/worker.js** — single-file, zero-dependency Worker.
  `POST /ingest` (authed) stores the blob in KV; `GET /badge.svg` renders it.
- **README embed** — `<img src="https://<worker>.workers.dev/badge.svg">`.

## Data sources (all local, read-only)

| Metric | Source |
|---|---|
| Claude Code tokens/cost | `~/.claude/projects/**/*.jsonl` — sum `message.usage`, dedup by `(message.id, requestId)`, cost from a public pricing table |
| Codex tokens | `~/.codex/sessions/**/*.jsonl` — last `total_token_usage` per session file (cumulative), summed |
| Claude sub 5h/weekly % | Anthropic OAuth **usage-reporting** endpoint (`api.anthropic.com/api/oauth/usage`, fields `five_hour.utilization` / `seven_day.utilization`) using the local Claude Code login. Read-only, zero-cost, never runs a model; the credential is never uploaded. If the stored access token is expired, the collector refreshes it via `platform.claude.com/v1/oauth/token` with the stored refresh token (Claude Code's own client id) and writes the rotated tokens back to `~/.claude/.credentials.json` so Claude Code stays in sync. |
| Codex usage % (live) | [CodexBar](https://github.com/steipete/codexbar) (`brew install codexbar`): `codexbar usage --provider codex --source oauth --format json`. Uses your Codex OAuth token against OpenAI's official usage endpoint — returns the live window(s) (`usedPercent` + `windowMinutes`, e.g. monthly on Pro-Lite), not the stale per-session log snapshots. Bar labels are derived from `windowMinutes` (300→5h, 10080→wk, 43200→mo) so plan changes don't break the badge. Used only when CodexBar is installed; absent → no Codex bar. |
| Ollama tokens | Local metering proxy (`collector/ollama_meter.py`). Ollama keeps no token log and Ollama Cloud has no usage API, so the proxy sits in front of Ollama, forwards requests unchanged, and tallies the `prompt_eval_count`/`eval_count` Ollama already returns into `~/.config/usage-badge/ollama-tally.json`. Point your apps' Ollama URL at the proxy to meter them. Counts integers only — never reads or stores prompts/completions. |
| Other providers | `~/.config/usage-badge/extra.json` — hand-maintained entries for anything with no readable source. |
| Other agents (ollama, claude api, …) | optional `extra.json` next to the collector — you fill in numbers however you like |

## Ingest payload (the only thing that leaves your machine)

```json
{
  "sub": [
    {"label": "claude 5h", "pct": 6.0},
    {"label": "claude wk", "pct": 3.0},
    {"label": "codex mo",  "pct": 100}
  ],
  "agents": [
    {"label": "claude code", "tokens": 1234567890, "cost_usd": 312.4},
    {"label": "codex",       "tokens": 111222333,  "cost_usd": 41.0},
    {"label": "ollama",      "tokens": 656,         "cost_usd": 0.0}
  ]
}
```

`sub` is a flexible list of labeled bars (window labels are collector-supplied,
so 5h/weekly/monthly plan differences never break rendering). The worker
sanitizes every field: pct clamped 0–100, labels whitelisted, list lengths
capped. Costs are estimates from public pricing tables; the badge labels them
"est".

## Worker behavior

- `POST /ingest` — requires `Authorization: Bearer <INGEST_TOKEN>` (Wrangler
  secret, constant-time compare). Body capped at 4 KB. The payload is
  **re-built field by field**: numbers are `Number()`-coerced and clamped,
  labels whitelisted to `[a-z0-9 ._-]{1,24}`, agent list capped at 6 entries.
  Anything else is dropped. The sanitized blob + server timestamp go to KV.
- `GET /badge.svg` — renders KV blob as SVG. `Cache-Control: max-age=600`.
  States: **no data yet** (fresh deploy), **live**, **stale** (>24 h old —
  shows a stale notice instead of silently lying).
- `GET /` — redirects to the repo.
- Ingest is rate-limited (min 60 s between accepted writes, and failed auth
  responses are delayed) to blunt token guessing.

## Security review

1. **No secrets in the repo.** Token is a Wrangler secret; collector reads it
   from `~/.config/usage-badge/token` (chmod 600). `wrangler.toml` names it only.
2. **Minimal blast radius.** The worker can read nothing of yours. If the
   ingest token leaks, the worst case is a vandalized badge — rotate the
   secret (`wrangler secret put INGEST_TOKEN`) and move on.
3. **No injection surface.** SVG is built only from clamped numbers and
   whitelisted label characters; no raw string ever reaches the markup. `<` `>`
   `&` `"` cannot appear in any rendered value.
4. **No data exfiltration.** The collector's network activity is exactly two
   requests: one GET to `api.anthropic.com` (usage %) and one POST to your own
   worker. Easy to audit — it's ~200 lines of stdlib Python.
5. **Privacy is a knob.** Publishing $ figures is a deliberate choice here;
   hobbyists who want less can just omit `cost_usd` (badge hides the column).

## Edge cases considered

- **Fresh deploy, no ingest yet** → "no data yet" badge, not an error.
- **Collector dies / laptop asleep >24 h** → "stale" badge state.
- **Claude OAuth call fails** (token expired because you're on Claude API, or
  endpoint change) → Claude sub bars simply don't render; tokens/cost still do.
- **Codex not installed** → its token row is omitted entirely.
- **Codex usage % is unavailable by design** (see data-sources note). We never
  scrape stale rate-limit snapshots. Only Codex token totals are shown.
- **Huge token counts** → human formatting (`1.2B`), values clamped to
  sane ranges (pct 0–100, tokens < 1e15, cost < 1e7).
- **NaN / negative / string-typed numbers in payload** → dropped by the
  sanitizer, never rendered.
- **GitHub camo caching** → 10-min cache header keeps camo reasonably fresh.
- **Dark & light READMEs** → self-contained dark card with border; legible on
  both GitHub themes.
- **Two machines pushing** → last write wins by design (KV `latest` key);
  documented, fine for a toy.
- **Clock skew** → staleness uses the *worker's* receive time, not the
  collector's clock.
- **Large/append-heavy JSONL logs** → collector keeps a per-file
  `(size, mtime) → totals` cache so reruns only parse changed files.
- **Pricing drift** → table lives in one dict at the top of the collector with
  a "last checked" date; costs always labeled "est".
- **Malformed JSONL lines** → skipped silently (agent logs contain many
  non-usage record types).
- **KV eventual consistency** → a just-pushed update may take ~60 s to show
  globally; irrelevant at this cadence.

## Non-goals (YAGNI)

History/graphs, multiple users, auth on the read path, per-model breakdowns,
a database. It's a badge.
