"""One hub cycle: run every collector that is due, then publish to R2.

    python -m tools.run                       # core hub: due collectors, prune, publish files + snapshot
    python -m tools.run --domain procurement  # one domain: its own database, its own files (no snapshot)
    python -m tools.run --all                 # every collector of the chosen hub regardless of interval
    python -m tools.run --no-publish          # collect only (local testing without Cloudflare)
    python -m tools.run --out DIR             # publish to a local folder instead of R2
    python -m tools.run --republish-all       # core: rebuild and re-upload every monthly partition
    python -m tools.run eirgrid_live          # named collector(s)
    python -m tools.run --list                # collectors by domain, with interval, tier and rights
"""
from __future__ import annotations

import os
import sys

# The domain must be known before the database engine is created, so read it first.
_argv = sys.argv[1:]
if "--domain" in _argv:
    os.environ["HUB_DOMAIN"] = _argv[_argv.index("--domain") + 1]

from datetime import timedelta  # noqa: E402

from sqlalchemy import delete  # noqa: E402

from s33hub.collectors import CORE, DOMAINS, REGISTRY, for_domain, last_run, make_client, run_collector  # noqa: E402
from s33hub.config import settings  # noqa: E402
from s33hub.db import SessionLocal, init_db, utcnow  # noqa: E402
from s33hub.models import ObservationRow  # noqa: E402
from s33hub.publish import DiskUploader, Uploader, publish  # noqa: E402


def due(session, key: str) -> bool:
    prev = last_run(session, key)
    if prev is None or not prev.ok:
        return True
    return utcnow() - prev.started_at >= timedelta(hours=REGISTRY[key].interval_hours) - timedelta(minutes=10)


def listing() -> int:
    for dom in [CORE] + DOMAINS:
        print(f"[{dom}]")
        for k, c in for_domain(dom).items():
            print(f"  {k:26} every {c.interval_hours:>5g} h  {c.tier:6} rights={c.rights:5} {c.publisher}")
    return 0


def main(argv: list[str]) -> int:
    if "--list" in argv:
        return listing()
    domain = settings.domain
    if domain != CORE and domain not in DOMAINS:
        print(f"unknown domain {domain!r}; available: {', '.join([CORE] + DOMAINS)}")
        return 2
    mine = for_domain(domain)
    skip = {argv[i + 1] for i, a in enumerate(argv) if a in ("--domain", "--out")}
    keys = [a for a in argv if not a.startswith("--") and a not in skip]
    unknown = [k for k in keys if k not in mine]
    if unknown:
        print(f"unknown collector(s) for domain {domain}: {', '.join(unknown)}; available: {', '.join(mine)}")
        return 2
    init_db()
    failed = 0
    run_keys = keys or list(mine)
    with SessionLocal() as s, make_client() as client:
        for k in run_keys:
            if not keys and "--all" not in argv and not due(s, k):
                print(f"{k:26} skipped (not due)")
                continue
            run = run_collector(s, mine[k], client)
            print(f"{k:26} {'ok    ' if run.ok else 'FAILED'} fetched={run.fetched} new={run.created} changed={run.changed}  {run.message}")
            failed += 0 if run.ok else 1
        if domain == CORE:
            s.execute(delete(ObservationRow).where(ObservationRow.observed_at < utcnow() - timedelta(days=400)))
            s.commit()
        if "--no-publish" not in argv:
            out = argv[argv.index("--out") + 1] if "--out" in argv else None
            info = publish(s, DiskUploader(out) if out else Uploader(), months_back=None if "--republish-all" in argv else 2, domain=domain)
            print(f"publish [{domain}]   {info['uploaded']} uploaded, {info['skipped']} unchanged of {info['files']}; snapshot {info['snapshot_bytes'] / 1e6:.1f} MB")
    return 1 if failed and failed == len(run_keys) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
