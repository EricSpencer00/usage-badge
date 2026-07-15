# usage-badge

A tiny self-hosted README badge showing your AI agent usage: Claude
subscription window usage (5-hour / weekly %, when you're on the sub), plus
estimated tokens and cost across your agents (Claude Code, Codex, and anything
you add). A toy — one Cloudflare Worker (free tier), one stdlib-only Python
script, no dependencies anywhere.

> **Why no Codex %?** Codex has no clean live usage endpoint — its rate-limit
> numbers are stale per-session snapshots on the wrong window. Rather than show
> misleading data, the badge reports Codex *token totals* only. See
> [docs/spec.md](docs/spec.md).

![usage badge](https://usage-badge.stockgenie.workers.dev/badge.svg)

## How it works

A collector script on **your** machine reads your local agent logs (read-only)
and pushes a ~1 KB blob of aggregate numbers to a Cloudflare Worker, which
renders it as an SVG your README embeds. That blob — percentages, token
counts, estimated dollars — is the only data that ever leaves your machine.
No prompts, no file paths, no credentials. The collector is ~200 lines of
Python you can read in five minutes: [collector/collect.py](collector/collect.py).

```
your machine ──POST (bearer token)──▶ worker + KV ──GET /badge.svg──▶ README
```

Full design + security notes + edge cases: [docs/spec.md](docs/spec.md).

## Host your own

1. **Deploy the worker**
   ```sh
   cd worker
   wrangler kv namespace create USAGE     # paste the id into wrangler.toml
   wrangler deploy
   openssl rand -hex 32 | tee /tmp/t | wrangler secret put INGEST_TOKEN
   ```
2. **Configure the collector**
   ```sh
   mkdir -p ~/.config/usage-badge && chmod 700 ~/.config/usage-badge
   echo 'https://usage-badge.<your-acct>.workers.dev/ingest' > ~/.config/usage-badge/url
   cp /tmp/t ~/.config/usage-badge/token && rm /tmp/t && chmod 600 ~/.config/usage-badge/token
   python3 collector/collect.py --dry-run   # see what would be sent
   python3 collector/collect.py             # push it
   ```
3. **Schedule it** (macOS): edit the path in `collector/com.usage-badge.plist`,
   then `cp` it to `~/Library/LaunchAgents/` and `launchctl load` it.
   (Linux: a cron line `*/30 * * * * python3 .../collect.py` does the same.)
4. **Embed it**
   ```md
   ![usage](https://usage-badge.<your-acct>.workers.dev/badge.svg)
   ```

Want tokens but not dollars on a public profile? Omit `cost_usd` — the badge
hides the column. Extra agents (ollama, raw API keys, anything) go in
`~/.config/usage-badge/extra.json`.

## Security, briefly

- No secrets in this repo; the ingest token is a Wrangler secret + a
  chmod-600 local file.
- The worker sanitizes every field (numbers clamped, labels whitelisted) —
  nothing from the wire reaches the SVG raw.
- Worst case if your token leaks: someone vandalizes your badge. Rotate with
  `wrangler secret put INGEST_TOKEN`.
- Costs are estimates from public pricing tables and labeled "est".

MIT license.
