# Adding a source to the hub

A source is one `Collector` class. Most take 50-150 lines. Follow these steps in order.

## 1. Decide the domain and tier
- **Domain**: the job that runs it. Pick an existing one (`procurement`, `climate`, `environment`, `statistics`, `property`, `transport`, `governance`, `energy_intl`, `catalogue`) or add a new one: a new domain needs a line in `.github/workflows/hub-domains.yml` (a test fails until it is there). Do not put new sources in `core`: it is the original hourly hub and the only one with a shared snapshot.
- **Tier**: `files` for tables and documents published as files (default choice); `rows` when data accumulates and must be queried or folded together (readings that are only available as "latest"); `mirror` for an untouched copy a site parses itself.
- **Rights**: `open` only if you have seen the licence (put its name in `licence`). Otherwise `rights = "check"`.

## 2. Write the collector
```python
class MySource(Collector):
    key = "my_source"                       # unique, lowercase
    name = "What it is, in words"
    publisher = "Who publishes it"
    url = "https://landing-page"
    licence = "Creative Commons Attribution 4.0"
    provides = "One sentence: what a site gets from it."
    interval_hours = 24.0
    domain, tier = "statistics", "files"
    used_by = ("somesite",)

    def run(self, session, client):
        state = load_state(session, self.key); items = state.setdefault("items", {})
        raw = get_with_retry(client, URL, timeout=300).content
        log_fetch(session, self.key, URL, sha256(raw), len(raw), self.licence)     # provenance: every payload
        publish_table(items, "table", "v1/<family>/<name>.csv.gz", gz(csv_bytes(...)), "application/gzip", n_rows, "description", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.message = "what happened this run"
        return res
```
Helpers are in `collectors/tables.py`: `publish_table` (publishes only if the bytes changed), `load_state`/`save_state` (a small JSON record that persists between runs), `add_daily` + `daily_files` (fold "latest" readings into daily min/max/mean). A collector may also define `files(session)` to publish files built from its database.

Rules that keep the hub trustworthy:
- **Log every payload** with `log_fetch`; never publish a figure you cannot trace to a fetch.
- **Fail loudly, lose nothing.** Raise if the source looks wrong (a layout change, an implausibly small file). In a multi-item source, one failing item is recorded in the message and the rest continue; all failing is an error.
- **Stay polite.** Check `Last-Modified`/`Content-Length` before downloading big files; do not poll faster than the source changes.
- **Minimise personal data.** If a source includes personal data that a use does not need, drop it at collection (see the Property Price Register: street address removed).
- Keep `used_by` and `provides` accurate: they appear in the catalogue.

## 3. Register and test
1. Import it and add it to `REGISTRY` in `collectors/__init__.py`.
2. Add a parser test with a realistic sample in `tests/test_domains.py` (the contract test already checks your metadata).
3. Run it for real, locally, to a folder: `HUB_DATA_DIR=... python -m tools.run --domain <domain> <key> --all --out ./out`, and look at the files.
4. `python -m tools.sources_doc` to refresh `docs/SOURCES.md`.

## 4. What publishing does
Per domain, each run uploads (in parallel batches) only files whose SHA-256 changed, plus `v1/catalog/<domain>.json`, `v1/status/<domain>.json`, `v1/events/<domain>.json` and the ledger. The Worker serves `/v1/catalog.json` and `/v1/status.json` as the merge of all domains, so readers see one hub.

## Limits to remember
R2 free tier is 10 GB. A workflow job has 40 minutes: refresh large families in chunks (see `climate.py`). The repo is public: only public, reusable data belongs in the hub.
