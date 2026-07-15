// usage-badge worker — single file, zero dependencies.
// POST /ingest (bearer-authed) stores a small sanitized usage blob in KV.
// GET  /badge.svg renders it. GET / redirects to the repo.

const REPO_URL = "https://github.com/EricSpencer00/usage-badge";
const MAX_BODY = 4096;
const STALE_MS = 24 * 60 * 60 * 1000;
const MIN_WRITE_GAP_MS = 60 * 1000;

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === "/ingest" && request.method === "POST") return ingest(request, env);
    if (pathname === "/badge.svg") return badge(env);
    return Response.redirect(REPO_URL, 302);
  },
};

// --- ingest ---------------------------------------------------------------

async function ingest(request, env) {
  const auth = request.headers.get("Authorization") || "";
  const ok = env.INGEST_TOKEN && (await timingSafeEqual(auth, `Bearer ${env.INGEST_TOKEN}`));
  if (!ok) {
    await new Promise((r) => setTimeout(r, 500)); // blunt brute force
    return new Response("unauthorized", { status: 401 });
  }

  const prev = await env.USAGE.get("latest", "json");
  if (prev && Date.now() - prev.updated_at < MIN_WRITE_GAP_MS)
    return new Response("too soon", { status: 429 });

  const text = await request.text();
  if (text.length > MAX_BODY) return new Response("too large", { status: 413 });
  let raw;
  try {
    raw = JSON.parse(text);
  } catch {
    return new Response("bad json", { status: 400 });
  }

  // Rebuild the payload field by field; nothing from the wire is trusted.
  const data = {
    // sub: flexible list of labeled usage bars (windows vary by plan, so labels
    // are collector-supplied rather than hardcoded). e.g. [{label,pct}].
    sub: (Array.isArray(raw?.sub) ? raw.sub : [])
      .slice(0, 5)
      .map((b) => ({
        label: String(b?.label ?? "").toLowerCase().replace(/[^a-z0-9 ._-]/g, "").slice(0, 16),
        pct: pct(b?.pct),
      }))
      .filter((b) => b.label && b.pct != null),
    agents: (Array.isArray(raw?.agents) ? raw.agents : [])
      .slice(0, 6)
      .map((a) => ({
        label: String(a?.label ?? "").toLowerCase().replace(/[^a-z0-9 ._-]/g, "").slice(0, 24),
        tokens: clamp(a?.tokens, 0, 1e15),
        cost_usd: a?.cost_usd == null ? null : clamp(a.cost_usd, 0, 1e7),
      }))
      .filter((a) => a.label && a.tokens != null),
    updated_at: Date.now(),
  };

  await env.USAGE.put("latest", JSON.stringify(data));
  return new Response("ok");
}

function clamp(v, lo, hi) {
  if (v == null || v === "") return null; // Number(null) is 0 — don't fake zeros
  const n = Number(v);
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : null;
}
const pct = (v) => clamp(v, 0, 100);

async function timingSafeEqual(a, b) {
  const enc = new TextEncoder();
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const va = new Uint8Array(ha), vb = new Uint8Array(hb);
  let diff = 0;
  for (let i = 0; i < va.length; i++) diff |= va[i] ^ vb[i];
  return diff === 0;
}

// --- badge ----------------------------------------------------------------

async function badge(env) {
  const data = await env.USAGE.get("latest", "json");
  const svg = renderSvg(data);
  return new Response(svg, {
    headers: {
      "Content-Type": "image/svg+xml",
      "Cache-Control": "public, max-age=600",
    },
  });
}

// Layout: dark card, left column = subscription bars, right column = agent totals.
const W = 720, H = 84;
const C = {
  bg: "#0d1117", border: "#30363d", text: "#e6edf3", dim: "#8b949e",
  track: "#21262d", ok: "#3fb950", warn: "#d29922", hot: "#f85149", accent: "#58a6ff",
};

function renderSvg(data) {
  const parts = [];
  if (!data) {
    parts.push(txt(W / 2, H / 2 + 4, "no data yet", C.dim, 13, "middle"));
    return wrap(parts);
  }
  const ageMs = Date.now() - data.updated_at;
  const stale = ageMs > STALE_MS;

  // left: subscription usage bars (collector-supplied labels)
  const bars = (Array.isArray(data.sub) ? data.sub : []).slice(0, 5);

  parts.push(txt(16, 22, "subscription usage", C.dim, 10));
  if (bars.length === 0) parts.push(txt(16, 44, "—", C.dim, 12));
  bars.forEach((b, i) => {
    const y = 33 + i * 13;
    parts.push(txt(16, y + 8, b.label, C.text, 10));
    parts.push(bar(88, y, 150, 7, b.pct, stale));
    parts.push(txt(246, y + 8, `${Math.round(b.pct)}%`, C.dim, 10));
  });

  // right: per-agent tokens + cost
  const x = 300;
  parts.push(txt(x, 22, "tokens · cost (est, all-time)", C.dim, 10));
  const agents = data.agents.slice(0, 4);
  if (agents.length === 0) parts.push(txt(x, 44, "—", C.dim, 12));
  agents.forEach((a, i) => {
    const y = 41 + i * 13;
    parts.push(txt(x, y, a.label, C.text, 10));
    parts.push(txt(x + 110, y, fmtTokens(a.tokens), C.accent, 10));
    if (a.cost_usd != null) parts.push(txt(x + 180, y, `$${fmtNum(a.cost_usd)}`, C.dim, 10));
  });
  const total = agents.reduce((s, a) => s + (a.cost_usd || 0), 0);
  if (total > 0) {
    parts.push(txt(x + 260, 41, "total", C.dim, 10));
    parts.push(txt(x + 260, 54, `$${fmtNum(total)}`, C.text, 12));
  }

  // footer: freshness
  const when = stale ? `stale — last update ${fmtAge(ageMs)} ago` : `updated ${fmtAge(ageMs)} ago`;
  parts.push(txt(W - 16, H - 10, when, stale ? C.warn : C.dim, 9, "end"));
  return wrap(parts);
}

function bar(x, y, w, h, v, stale) {
  const fillW = Math.round((w * v) / 100);
  const color = stale ? C.dim : v >= 90 ? C.hot : v >= 70 ? C.warn : C.ok;
  return (
    `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="3.5" fill="${C.track}"/>` +
    (fillW > 0 ? `<rect x="${x}" y="${y}" width="${Math.max(fillW, 4)}" height="${h}" rx="3.5" fill="${color}"/>` : "")
  );
}

function txt(x, y, s, fill, size, anchor = "start") {
  return `<text x="${x}" y="${y}" fill="${fill}" font-size="${size}" text-anchor="${anchor}">${s}</text>`;
}

function wrap(parts) {
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="AI agent usage">` +
    `<rect width="${W}" height="${H}" rx="8" fill="${C.bg}" stroke="${C.border}"/>` +
    `<g font-family="ui-monospace,SFMono-Regular,Menlo,monospace">${parts.join("")}</g></svg>`
  );
}

function fmtTokens(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
}
function fmtNum(n) {
  return n >= 100 ? Math.round(n).toLocaleString("en-US") : n.toFixed(2);
}
function fmtAge(ms) {
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}
