#!/usr/bin/env python3
"""ollama_meter — a tiny local metering proxy for Ollama.

Ollama keeps no persistent token log, so there's nothing for the badge to read.
This proxy sits in front of Ollama (local daemon or Ollama Cloud), forwards
every request unchanged, and tallies the token counts that Ollama already
returns in each response (`prompt_eval_count`, `eval_count`). The running total
is written to ~/.config/usage-badge/ollama-tally.json, which the collector reads.

It is a transparent pass-through: it does not read, store, or alter your prompts
or completions — only the integer token counts from the response envelope. The
Authorization header (for Ollama Cloud) is forwarded as-is and never logged.

Usage:
  OLLAMA_TARGET=http://localhost:11434 python3 ollama_meter.py   # local (default)
  OLLAMA_TARGET=https://ollama.com     python3 ollama_meter.py   # cloud
Then point your apps at http://localhost:11435 instead of the real Ollama URL.

Env:
  OLLAMA_TARGET   upstream base URL (default http://localhost:11434)
  METER_PORT      listen port (default 11435)
"""
import json, os, sys, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TARGET = os.environ.get("OLLAMA_TARGET", "http://localhost:11434").rstrip("/")
PORT = int(os.environ.get("METER_PORT", "11435"))
TALLY = Path.home() / ".config" / "usage-badge" / "ollama-tally.json"
# Ollama Cloud is credit-based/free today; set a per-Mtoken rate if you want a
# cost estimate. Local models are free. Tokens are always counted regardless.
COST_PER_MTOK = float(os.environ.get("OLLAMA_COST_PER_MTOK", "0"))


def load_tally():
    try:
        return json.loads(TALLY.read_text())
    except Exception:
        return {"input_tokens": 0, "output_tokens": 0, "tokens": 0,
                "cost_usd": 0.0, "by_model": {}, "requests": 0}


def save_tally(t):
    TALLY.parent.mkdir(parents=True, exist_ok=True)
    tmp = TALLY.with_suffix(".tmp")
    tmp.write_text(json.dumps(t))
    os.replace(tmp, TALLY)


def record(model, pin, pout):
    t = load_tally()
    t["input_tokens"] += pin
    t["output_tokens"] += pout
    t["tokens"] = t["input_tokens"] + t["output_tokens"]
    t["requests"] += 1
    t["cost_usd"] = round(t["tokens"] * COST_PER_MTOK / 1e6, 4)
    m = t["by_model"].setdefault(model or "unknown", {"tokens": 0})
    m["tokens"] += pin + pout
    save_tally(t)


def counts_from(obj):
    """Pull (model, prompt_eval_count, eval_count) from an Ollama response obj."""
    if not isinstance(obj, dict):
        return None
    if "prompt_eval_count" in obj or "eval_count" in obj:
        return (obj.get("model", ""),
                int(obj.get("prompt_eval_count", 0) or 0),
                int(obj.get("eval_count", 0) or 0))
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # stay quiet; no prompt/response content ever logged

    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        url = TARGET + self.path
        req = urllib.request.Request(url, data=body, method=self.command)
        for h in ("Content-Type", "Authorization", "Accept"):
            if h in self.headers:
                req.add_header(h, self.headers[h])
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
            return
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())
            return

        self.send_response(resp.status)
        # stream body back chunked; tee each line to the counter
        ctype = resp.headers.get("Content-Type", "application/json")
        self.send_header("Content-Type", ctype)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        model = ""; pin = pout = 0
        buf = b""
        client_open = True
        for chunk in resp:
            if client_open:
                try:  # keep counting even if the client hung up mid-stream
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    client_open = False
            buf += chunk
            # Ollama streams newline-delimited JSON; final object carries counts.
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                c = self._parse(line)
                if c:
                    model, pin, pout = c
        # non-streaming: whole body is one JSON object
        c = self._parse(buf)
        if c:
            model, pin, pout = c
        if client_open:
            try:
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
        if pin or pout:
            try:
                record(model, pin, pout)
            except Exception:
                pass

    @staticmethod
    def _parse(line):
        line = line.strip()
        if not line:
            return None
        try:
            return counts_from(json.loads(line))
        except Exception:
            return None

    do_GET = _proxy
    do_POST = _proxy
    do_DELETE = _proxy
    do_PUT = _proxy


if __name__ == "__main__":
    print(f"ollama_meter: :{PORT} -> {TARGET} (tally: {TALLY})", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
