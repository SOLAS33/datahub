// node worker/test/merge.test.mjs : the merged catalogue and status are what the domains published, with no loss and no duplicates.
import assert from "node:assert/strict";
import { mergeCatalogs, mergeStatuses } from "../worker.js";

const core = { generated_at: "2026-10-05T10:00:00Z", domain: "core", licence: "L", publisher: "P", base: "B", files: [{ path: "v1/eirgrid/a.csv" }, { path: "v1/shared.csv", sha256: "old" }] };
const proc = { generated_at: "2026-10-05T11:00:00Z", domain: "procurement", files: [{ path: "v1/tenders/x.csv.gz" }, { path: "v1/shared.csv", sha256: "new" }] };
const m = mergeCatalogs([core, proc]);
assert.deepEqual(m.domains, ["core", "procurement"]);
assert.equal(m.generated_at, "2026-10-05T11:00:00Z");
assert.deepEqual(m.files.map((f) => f.path), ["v1/eirgrid/a.csv", "v1/shared.csv", "v1/tenders/x.csv.gz"]);
assert.equal(m.files.find((f) => f.path === "v1/shared.csv").sha256, "new");
assert.equal(m.licence, "L");

const s = mergeStatuses([{ domain: "procurement", generated_at: "b", sources: [{ key: "ted", domain: "procurement" }] }, { domain: "core", generated_at: "a", sources: [{ key: "eirgrid_live", domain: "core" }] }]);
assert.deepEqual(s.sources.map((x) => x.key), ["eirgrid_live", "ted"]);
assert.equal(s.generated_at, "b");
assert.deepEqual(mergeCatalogs([]).files, []);
console.log("merge tests ok");

// the R2-backed page cache: fresh copies are served as they are; stale ones are served at once and rebuilt in the background;
// when building fails the old copy still answers; with no copy and a failing build the answer is a clean failure, not a crash
import { viaR2Cache } from "../worker.js";
const store = new Map();
const env = { HUB: { async get(k) { const o = store.get(k); return o ? { body: o.body, customMetadata: o.meta } : null; }, async put(k, body, o) { store.set(k, { body, meta: o.customMetadata }); } } };
const pending = [];
const ctx = { waitUntil: (p) => pending.push(p) };
let builds = 0;
const build = async () => { builds++; return "page v" + builds; };
const T0 = Date.parse("2026-10-14T10:00:00Z");
let r = await viaR2Cache(env, ctx, "index.html", 900000, build, "text/html", T0);
assert.equal(r.body, "page v1"); assert.equal(builds, 1);
r = await viaR2Cache(env, ctx, "index.html", 900000, build, "text/html", T0 + 60000);
assert.equal(r.body, "page v1"); assert.equal(builds, 1, "fresh copy: no rebuild");
r = await viaR2Cache(env, ctx, "index.html", 900000, build, "text/html", T0 + 1000000);
assert.equal(r.body, "page v1"); assert.equal(r.stale, true, "stale copy served immediately");
await Promise.all(pending);
assert.equal(builds, 2, "and rebuilt in the background");
assert.equal(store.get("_cache/index.html").body, "page v2");
const bad = async () => { throw new Error("cpu"); };
r = await viaR2Cache(env, ctx, "index.html", 900000, bad, "text/html", T0 + 5000000);
assert.equal(r.body, "page v2", "build failed: the last good copy still answers"); await Promise.all(pending);
r = await viaR2Cache(env, ctx, "other.json", 900000, bad, "application/json", T0);
assert.equal(r.body, null); assert.ok(r.error);
console.log("cache tests ok");
