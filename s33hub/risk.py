"""Company risk indicators from public register data (rules version 1.0).

This turns the Companies Registration Office's open data (status, registration, annual-return and accounts dates, accounts filings) into
a small set of plain, documented flags per company, plus the market-level tables a risk site needs. It is evidence from public records,
not a credit score: it cannot see turnover, debts, directors or court action, and a flag may reflect data lag or an exemption rather
than trouble. Every flag states the rule that raised it.

Levels: red (insolvency process or listed for strike-off), amber (filing or newness indicators), green (no flag), grey (not a live company).
Rule thresholds are constants below so they can be argued with, tested and changed in one place.
"""
from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from datetime import date, timedelta

RULES_VERSION = "1.1"
AR_OVERDUE_MONTHS = 16          # annual return due within ~56 days of its date: older than 16 months is clearly overdue
ACCOUNTS_STALE_MONTHS = 30      # period end of the latest accounts on record; typical is 12-21 months, so 30 is well beyond normal
LATE_DAYS = 300                 # accounts received more than ~10 months after their period end
NEW_MONTHS = 12
NAME_CHANGE_MONTHS = 12
NEW_AND_LARGE_VALUE = 250_000   # a company under a year old that has won this much in sole public awards
MIN_AGE_FOR_FILING_RULES_MONTHS = 18
SHARD_SIZE = 1000
SEARCH_SPLIT = 5000             # a 2-letter search shard bigger than this is split by 3 letters

GROUPS = {"L": "live", "I": "insolvency", "S": "strike-off listed", "D": "dissolved or ceased"}


def months_between(a: date, b: date) -> float:
    return (b - a).days / 30.44


def d(v: str) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def status_group(status: str) -> str:
    """L live, I insolvency process, S listed for strike-off, D dissolved/ceased/removed."""
    s = (status or "").strip().lower()
    if s == "normal":
        return "L"
    if s.startswith(("liquidation", "administration", "examinership", "receiver")):
        return "I"
    if s == "strike off listed":
        return "S"
    return "D"


def assess(c: dict, today: date, filings: list[tuple[str, str]] | None = None, contracts: dict | None = None, sanctions: dict | None = None) -> tuple[list[list[str]], str]:
    """(flags, level) for one company record. A flag is [code, severity, text]."""
    flags: list[list[str]] = []
    g = c["sg"]
    reg, sd = d(c["rg"]), d(c["sd"])
    age = months_between(reg, today) if reg else None
    if g == "I":
        flags.append(["INSOLVENCY", "red", f"In an insolvency process on the register: {c['st']}" + (f" (since {c['sd']})" if c["sd"] else "")])
    elif g == "S":
        flags.append(["STRIKE_OFF", "red", "Listed for strike-off" + (f" since {c['sd']}" if c["sd"] else "") + ": the company is on course to be dissolved unless the listing is resolved"])
    elif g == "D":
        flags.append(["NOT_LIVE", "grey", f"Not a live company: {c['st']}" + (f" (since {c['sd']})" if c["sd"] else "")])
    if g == "L":
        ar = d(c["ar"])
        if age is not None and age > MIN_AGE_FOR_FILING_RULES_MONTHS:
            if ar is None:
                flags.append(["AR_NONE", "amber", "No annual return on record"])
            elif months_between(ar, today) > AR_OVERDUE_MONTHS:
                flags.append(["AR_OVERDUE", "amber", f"Annual return overdue: the last one on record is dated {c['ar']} ({months_between(ar, today):.0f} months ago)"])
        ac = d(c["ac"])
        if age is not None and age > ACCOUNTS_STALE_MONTHS:
            if ac is None:
                flags.append(["ACCOUNTS_NONE", "amber", "No accounts on record (some companies are exempt or file in other ways)"])
            elif months_between(ac, today) > ACCOUNTS_STALE_MONTHS:
                flags.append(["ACCOUNTS_STALE", "amber", f"Latest accounts on record are to {c['ac']}, {months_between(ac, today):.0f} months ago"])
        fl = sorted(filings or [], key=lambda x: x[0], reverse=True)
        late = []
        for rec, to in fl[:2]:
            r, t = d(rec), d(to)
            late.append(bool(r and t and (r - t).days > LATE_DAYS))
        if len(late) == 2 and all(late):
            flags.append(["LATE_FILER", "amber", "The last two sets of accounts were filed more than 10 months after their period end"])
        if age is not None and age < NEW_MONTHS:
            flags.append(["NEW", "info", f"Registered {c['rg']}, under a year ago"])
        pc = contracts or None
        if pc and age is not None and age < NEW_MONTHS and (pc.get("v") or 0) >= NEW_AND_LARGE_VALUE:
            flags.append(["NEW_AND_LARGE", "amber", f"Under a year old but has won public contracts worth about EUR {pc['v']:,.0f}"])
    if g == "L" and sanctions:
        flags.append(["SANCTIONS_NAME", "amber", f"Possible sanctions name match: the company's name is identical to an entity on the EU financial sanctions list ({sanctions['n']}; {sanctions['p'] or 'programme not stated'}"
                      + (f"; designated {sanctions['d']}" if sanctions.get("d") else "") + "). A name match is not an identification: check the listed entity against this company before drawing any conclusion"])
    ne = d(c.get("ne", ""))
    if g == "L" and ne and ne > (reg or ne) and months_between(ne, today) < NAME_CHANGE_MONTHS:
        flags.append(["NAME_CHANGED", "info", f"Name changed on {c['ne']}"])
    sev = {f[1] for f in flags}
    level = "red" if "red" in sev else "grey" if "grey" in sev else "amber" if "amber" in sev else "green"
    return flags, level


def build_records(rows: list[list[str]], filings: dict[str, list[tuple[str, str]]], contracts: dict[str, dict], today: date, sanctions: dict[str, dict] | None = None) -> dict[str, dict]:
    """Per-company risk records from normalised CRO rows (see business.OUT column order)."""
    out: dict[str, dict] = {}
    for r in rows:
        num = r[0]
        if not num:
            continue
        rec = {"n": num, "nm": r[1], "st": r[2].strip(), "sg": status_group(r[2]), "ty": r[4], "rg": r[6], "ds": r[7], "sd": r[8], "ar": r[9], "ac": r[10], "nc": r[11], "ea": r[12],
               "ne": r[13] if len(r) > 13 else ""}
        fl = sorted(filings.get(num, []), reverse=True)
        rec["f"] = [[a, b, (d(a) - d(b)).days if d(a) and d(b) else None] for a, b in fl[:6]]
        pc = contracts.get(num)
        sx = (sanctions or {}).get(num)
        flags, level = assess(rec, today, fl, pc, sx)
        rec["fl"], rec["lv"], rec["pc"] = flags, level, pc
        if sx:
            rec["sx"] = sx
        out[num] = rec
    return out


def gz_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 9, mtime=0)


def shard_id(num: str) -> int:
    return int(num) // SHARD_SIZE if num.isdigit() else 0


def company_shards(records: dict[str, dict]) -> dict[str, bytes]:
    """v1/business/risk/company/<shard>.json.gz: {"v": rules version, "c": {number: record}}. No dates of generation inside, so a shard
    only changes when one of its companies changes."""
    by: dict[int, dict] = defaultdict(dict)
    for n, rec in records.items():
        by[shard_id(n)][n] = rec
    return {f"v1/business/risk/company/{s}.json.gz": gz_json({"v": RULES_VERSION, "c": cs}) for s, cs in by.items()}


def _key(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def search_shards(records: dict[str, dict]) -> dict[str, bytes]:
    """v1/business/risk/search/<prefix>.json.gz: [[number, name, status group, registration year, level], ...] for names starting with a 2-letter
    prefix (3 letters where that would be too big), plus search/index.json naming the split prefixes."""
    p2: dict[str, list] = defaultdict(list)
    for n, rec in records.items():
        k = _key(rec["nm"])
        if len(k) >= 1:
            p2[(k + "_")[:2]].append((k, [n, rec["nm"], rec["sg"], rec["rg"][:4], rec["lv"]]))
    out: dict[str, bytes] = {}
    split = []
    for pre, items in p2.items():
        if len(items) <= SEARCH_SPLIT:
            out[f"v1/business/risk/search/{pre}.json.gz"] = gz_json([e for _, e in sorted(items, key=lambda x: x[0])])
            continue
        split.append(pre)
        p3: dict[str, list] = defaultdict(list)
        for k, e in items:
            p3[(k + "__")[:3]].append((k, e))
        for pre3, its in p3.items():
            out[f"v1/business/risk/search/{pre3}.json.gz"] = gz_json([e for _, e in sorted(its, key=lambda x: x[0])])
    out["v1/business/risk/search/index.json"] = json.dumps({"split": sorted(split), "rules": RULES_VERSION}).encode()
    return out


def aggregates(records: dict[str, dict], today: date) -> dict[str, tuple[bytes, str]]:
    """Market-level tables: filing compliance by sector, status events by month, the strike-off list, recent insolvencies. {path: (bytes, type)}."""
    import csv
    import io

    def csv_gz(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        return gzip.compress(buf.getvalue().encode("utf-8"), 9, mtime=0)

    sec: dict[str, Counter] = defaultdict(Counter)
    ev: Counter = Counter()
    strike, insolv = [], []
    cutoff = today - timedelta(days=180)
    for rec in records.values():
        div = (rec["nc"] or "")[:2] or "??"
        c = sec[div]
        c["all"] += 1
        if rec["sg"] == "L":
            c["live"] += 1
            codes = {f[0] for f in rec["fl"]}
            c["ar_overdue"] += bool(codes & {"AR_OVERDUE", "AR_NONE"})
            c["accounts_stale"] += bool(codes & {"ACCOUNTS_STALE", "ACCOUNTS_NONE"})
            c["late_filer"] += "LATE_FILER" in codes
            c["new_12m"] += "NEW" in codes
        elif rec["sg"] == "I":
            c["insolvency"] += 1
        elif rec["sg"] == "S":
            c["strike_off"] += 1
        if rec["sd"] and rec["sg"] in ("I", "S", "D"):
            ev[(rec["sd"][:7], {"I": "insolvency", "S": "strike-off listed", "D": "dissolved or ceased"}[rec["sg"]])] += 1
        if rec["sg"] == "S":
            strike.append([rec["n"], rec["nm"], rec["sd"], rec["nc"], rec["ea"], rec["rg"], rec["ty"]])
        if rec["sg"] == "I" and rec["sd"] and rec["sd"] >= cutoff.isoformat():
            insolv.append([rec["n"], rec["nm"], rec["st"], rec["sd"], rec["nc"], rec["ea"], rec["rg"]])
    srows = [[div, c["live"], c["ar_overdue"], c["accounts_stale"], c["late_filer"], c["new_12m"], c["insolvency"], c["strike_off"]] for div, c in sorted(sec.items())]
    out = {
        "v1/business/risk/sector_filing.csv": (("nace_division,live_companies,annual_return_overdue,accounts_stale,late_filers,registered_last_12_months,in_insolvency,strike_off_listed\n"
                                               + "\n".join(",".join(str(x) for x in r) for r in srows) + "\n").encode(), "text/csv"),
        "v1/business/risk/status_events_monthly.csv.gz": (csv_gz(["month", "status_group", "companies"], [[m, g, c] for (m, g), c in sorted(ev.items())]), "application/gzip"),
        "v1/business/risk/strike_off_list.csv.gz": (csv_gz(["company_num", "name", "listed", "nace", "eircode_routing_key", "registered", "type"], sorted(strike, key=lambda r: r[2], reverse=True)), "application/gzip"),
        "v1/business/risk/recent_insolvencies.csv.gz": (csv_gz(["company_num", "name", "status", "since", "nace", "eircode_routing_key", "registered"], sorted(insolv, key=lambda r: r[3], reverse=True)), "application/gzip"),
        "v1/business/risk/meta.json": (json.dumps({"rules": RULES_VERSION, "companies": len(records), "live": sum(1 for r in records.values() if r["sg"] == "L"),
                                                    "thresholds": {"annual_return_overdue_months": AR_OVERDUE_MONTHS, "accounts_stale_months": ACCOUNTS_STALE_MONTHS, "late_filing_days": LATE_DAYS,
                                                                   "new_company_months": NEW_MONTHS, "new_and_large_value_eur": NEW_AND_LARGE_VALUE}}, indent=1).encode(), "application/json"),
    }
    return out
