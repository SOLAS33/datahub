// Solas33 Data Hub - serves the R2 bucket at https://data.solas33.com.
//   /                 human-readable index built from v1/catalog.json
//   /v1/...           files exactly as published (CORS open; edge-cached per the object's cache-control)
//   /v1/ (dir paths)  JSON listing of that prefix
// Read-only. Everything is public data compiled from public sources.

const CORS = { "access-control-allow-origin": "*", "access-control-allow-methods": "GET, HEAD, OPTIONS" };

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function index(env) {
  const obj = await env.HUB.get("v1/catalog.json");
  const cat = obj ? await obj.json() : { files: [] };
  const st = await env.HUB.get("v1/status.json");
  const status = st ? await st.json() : { sources: [] };
  const groups = {};
  for (const f of cat.files || []) {
    const g = f.path.split("/").slice(1, 3).join("/").replace(/\.(csv|json|gz)$/, "");
    (groups[g] = groups[g] || []).push(f);
  }
  const rows = Object.keys(groups).sort().map((g) => {
    const fs = groups[g].sort((a, b) => (a.path < b.path ? 1 : -1));
    const first = fs[0];
    const more = fs.length > 1 ? ` <span class="m">+${fs.length - 1} more (${fs.slice(1, 4).map((f) => `<a href="/${esc(f.path)}">${esc(f.path.split("/").pop())}</a>`).join(", ")}${fs.length > 4 ? ", …" : ""})</span>` : "";
    return `<tr><td><a href="/${esc(first.path)}">${esc(first.path.replace(/^v1\//, ""))}</a>${more}</td><td>${esc(first.description)}</td>` +
      `<td>${esc(first.publisher)}</td><td class="m">${esc((first.updated || "").slice(0, 16).replace("T", " "))}</td></tr>`;
  }).join("");
  const srcs = (status.sources || []).map((s) => `<tr><td>${esc(s.name)}</td><td>${esc(s.publisher)}</td><td>every ${esc(s.every_hours)} h</td>` +
    `<td><b class="${esc(s.state)}">${esc(s.state)}</b></td><td class="m">${esc((s.last_ok || "never").slice(0, 16).replace("T", " "))}</td>` +
    `<td class="m">${esc((s.used_by || []).join(", "))}</td></tr>`).join("");
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solas33 Data Hub</title><style>
:root{color-scheme:light dark;--bg:#f6f7f3;--fg:#1d2420;--mut:#6a736d;--line:#dfe3dc;--acc:#2f6f4f;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#121613;--fg:#e8ede9;--mut:#9aa59e;--line:#28302b;--acc:#7fc8a0;--card:#1a201c}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 16px}h1{margin:0 0 6px;font-size:1.8rem}a{color:var(--acc)}
.m{color:var(--mut);font-size:.85em}table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden;margin:12px 0 26px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:.78rem;text-transform:uppercase;color:var(--mut)}
.healthy{color:#1a8a3a}.stale{color:#b07800}.failing{color:#c0392b}code{background:var(--card);padding:2px 6px;border-radius:6px}
.wrap{overflow-x:auto}</style></head><body><main>
<h1>Solas33 Data Hub</h1>
<p>Public Irish energy, grid and weather data, collected once from the original publishers, stored with full provenance and shared by every Solas33 site.
Each file names its source; every payload's URL, retrieval time and SHA-256 are in the <a href="/v1/ledger/">ledger</a>. Machine-readable: <a href="/v1/catalog.json">catalog.json</a>,
<a href="/v1/status.json">status.json</a>, <a href="/v1/events.json">events.json</a>, full snapshot <a href="/v1/hub.sqlite.gz">hub.sqlite.gz</a>.</p>
<p class="m">${esc(cat.licence || "")} · Generated ${esc((cat.generated_at || "").slice(0, 16).replace("T", " "))} UTC · Used by <a href="https://gridwatch.solas33.com">GridWatch Ireland</a> and <a href="https://dcwatch.solas33.com">DCWatch Ireland</a>.</p>
<h2>Sources</h2><div class="wrap"><table><tr><th>Source</th><th>Publisher</th><th>Collected</th><th>Status</th><th>Last success (UTC)</th><th>Used by</th></tr>${srcs}</table></div>
<h2>Datasets</h2><div class="wrap"><table><tr><th>File</th><th>Contents</th><th>Original publisher</th><th>Updated (UTC)</th></tr>${rows}</table></div>
<p class="m">Code: <a href="https://github.com/SOLAS33/datahub">github.com/SOLAS33/datahub</a>. Contact: hello@solas33.com</p>
</main></body></html>`;
  return new Response(html, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=120", ...CORS } });
}

async function listing(env, prefix) {
  const out = [];
  let cursor;
  do {
    const r = await env.HUB.list({ prefix, cursor, limit: 1000 });
    for (const o of r.objects) out.push({ path: o.key, bytes: o.size, uploaded: o.uploaded, etag: o.etag });
    cursor = r.truncated ? r.cursor : undefined;
  } while (cursor && out.length < 5000);
  return new Response(JSON.stringify({ prefix, files: out }, null, 1), { headers: { "content-type": "application/json", "cache-control": "public, max-age=120", ...CORS } });
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("read-only", { status: 405, headers: CORS });
    const url = new URL(request.url);
    const key = decodeURIComponent(url.pathname.replace(/^\/+/, ""));
    if (key === "" || key === "index.html") return index(env);
    if (key.endsWith("/")) return listing(env, key);
    const cache = caches.default;
    const hit = await cache.match(request);
    if (hit) return hit;
    const obj = await env.HUB.get(key);
    if (!obj) return new Response(JSON.stringify({ error: "not found", path: key }), { status: 404, headers: { "content-type": "application/json", ...CORS } });
    const headers = new Headers(CORS);
    obj.writeHttpMetadata(headers);
    headers.set("etag", obj.httpEtag);
    if (!headers.get("cache-control")) headers.set("cache-control", "public, max-age=300");
    const resp = new Response(request.method === "HEAD" ? null : obj.body, { headers });
    if (request.method === "GET") ctx.waitUntil(cache.put(request, resp.clone()));
    return resp;
  },
};
