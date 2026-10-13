// Builds the Data Hub's public pages once, after data is published, so the Worker only has to hand over a stored file.
//   node tools/prebuild.mjs            reads the published parts from https://data.solas33.com, writes _pre/* into the R2 bucket
//   node tools/prebuild.mjs --dry-run  builds and prints sizes, uploads nothing
// Why: the index page, the merged catalogue and the v1/ listing take tens of milliseconds of CPU to build, and the free Workers plan allows
// ten, so building them per request made a share of requests fail. Here (in Actions) there is no such limit. The Worker serves _pre/* first
// and falls back to building (cached in R2) only if a pre-built copy is missing or over 26 hours old.
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { listingFromCatalog, mergeCatalogs, mergeStatuses, renderIndex } from "../worker/worker.js";

const BASE = process.env.HUB_BASE || "https://data.solas33.com";
const BUCKET = process.env.HUB_BUCKET || "solas33-datahub";
const DRY = process.argv.includes("--dry-run");

async function getJson(path) {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(`${BASE}/${path}`, { headers: { "user-agent": "solas33-datahub-prebuild" } });
      if (r.status === 404) return null;
      if (r.ok) return await r.json();
    } catch { /* retry */ }
    await new Promise((res) => setTimeout(res, 1500 * (i + 1)));
  }
  throw new Error(`could not read ${path}`);
}

async function parts(kind) {
  const listing = await getJson(`v1/${kind}/`);
  const out = [];
  for (const f of (listing && listing.files) || []) {
    if (!f.path.endsWith(".json")) continue;
    const j = await getJson(f.path);
    if (j) out.push(j);
  }
  if (!out.length) { const legacy = await getJson(`v1/${kind}.json`); if (legacy) out.push(legacy); }
  return out;
}

const catParts = await parts("catalog");
const statParts = await parts("status");
if (!catParts.length) { console.error("no catalogue parts found: not publishing a pre-built copy"); process.exit(1); }
const catalog = mergeCatalogs(catParts);
const status = statParts.length ? mergeStatuses(statParts) : { sources: [] };

// the hub's own files that are not data files (ledger, catalogue parts, snapshot, events), so the listing is complete
const extras = [];
for (const dir of ["v1/ledger/", "v1/catalog/", "v1/status/"]) {
  const l = await getJson(dir);
  for (const f of (l && l.files) || []) extras.push({ path: f.path, bytes: f.bytes, uploaded: f.uploaded, etag: f.etag });
}
for (const p of ["v1/events.json", "v1/hub.sqlite.gz", "v1/catalog.json", "v1/status.json"]) {
  try { const h = await fetch(`${BASE}/${p}`, { method: "HEAD" }); if (h.ok) extras.push({ path: p, bytes: Number(h.headers.get("content-length")) || null, uploaded: h.headers.get("last-modified") || null, etag: h.headers.get("etag") }); } catch { /* optional */ }
}

const outputs = [
  ["index.html", renderIndex(catalog, status), "text/html; charset=utf-8"],
  ["catalog.json", JSON.stringify(catalog), "application/json"],
  ["status.json", JSON.stringify(status), "application/json"],
  ["list/v1/.json", listingFromCatalog(catalog, extras), "application/json"],
];
const dir = mkdtempSync(join(tmpdir(), "prebuild-"));
for (const [name, body, type] of outputs) {
  console.log(`${name.padEnd(16)} ${(Buffer.byteLength(body) / 1024).toFixed(0)} KB`);
  if (DRY) continue;
  const f = join(dir, name.replace(/[\\/]/g, "_"));
  writeFileSync(f, body);
  const npx = process.platform === "win32" ? "npx.cmd" : "npx";
  execFileSync(npx, ["--yes", "wrangler@4", "r2", "object", "put", `${BUCKET}/_pre/${name}`, "--file", f, "--content-type", type, "--cache-control", "public, max-age=300", "--remote"], { stdio: "inherit" });
}
console.log(`${DRY ? "dry run: " : ""}${catalog.files.length} files in ${catalog.domains.length} domains`);
