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
