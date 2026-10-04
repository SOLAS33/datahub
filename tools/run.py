"""One hub cycle: run every collector that is due, prune, publish to R2.

    python -m tools.run                    # due collectors, then publish
    python -m tools.run --all              # every collector regardless of interval
    python -m tools.run --no-publish       # collect only (local testing without Cloudflare)
    python -m tools.run --republish-all    # rebuild and re-upload every monthly partition
    python -m tools.run eirgrid_live       # one collector
"""
from __future__ import annotations

import sys
from datetime import timedelta

from sqlalchemy import delete

from s33hub.collectors import REGISTRY, last_run, make_client, run_collector
from s33hub.db import SessionLocal, init_db, utcnow
from s33hub.models import ObservationRow
from s33hub.publish import Uploader, publish


def due(session, key: str) -> bool:
    prev = last_run(session, key)
    if prev is None or not prev.ok:
        return True
    return utcnow() - prev.started_at >= timedelta(hours=REGISTRY[key].interval_hours) - timedelta(minutes=10)


def main(argv: list[str]) -> int:
    keys = [a for a in argv if not a.startswith("--")]
    unknown = [k for k in keys if k not in REGISTRY]
    if unknown:
        print(f"unknown collector(s): {', '.join(unknown)}; available: {', '.join(REGISTRY)}")
        return 2
    init_db()
    failed = 0
    with SessionLocal() as s, make_client() as client:
        for k in keys or list(REGISTRY):
            if not keys and "--all" not in argv and not due(s, k):
                print(f"{k:20} skipped (not due)")
                continue
            run = run_collector(s, REGISTRY[k], client)
            print(f"{k:20} {'ok    ' if run.ok else 'FAILED'} fetched={run.fetched} new={run.created} changed={run.changed}  {run.message}")
            failed += 0 if run.ok else 1
        s.execute(delete(ObservationRow).where(ObservationRow.observed_at < utcnow() - timedelta(days=400)))
        s.commit()
        if "--no-publish" not in argv:
            info = publish(s, Uploader(), months_back=None if "--republish-all" in argv else 2)
            print(f"publish            {info['uploaded']} uploaded, {info['skipped']} unchanged of {info['files']}; snapshot {info['snapshot_bytes'] / 1e6:.1f} MB")
    return 1 if failed and failed == len(keys or REGISTRY) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
