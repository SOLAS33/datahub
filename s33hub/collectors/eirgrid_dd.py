"""EirGrid monthly dispatch-down (curtailment + constraint) workbook.

EirGrid republishes "DD-Summary-Report-V<n>.xlsx" on its System and Renewable Data Reports
page roughly monthly, with the version number in the file name. We read the page, find the
current link, and parse the workbook only when its file name or SHA-256 is new. Every figure
is stored with the workbook version, sheet and cell it came from.

Sheets used:
* "All RES DD Monthly Detailed"  - monthly availability / generation / dispatch-down for
  wind, solar and total renewables, for IE, NI and the all-island system.
* "Wind & Solar Monthly Detailed" - monthly wind dispatch-down split by cause.
* "Regional Wind & Solar"         - quarterly / annual dispatch-down % by region, and the
  list of controllable wind/solar farms with region, node and capacity.

Layouts were mapped from V22 (2026-10-04). Header text is checked before reading so a layout
change fails loudly instead of storing wrong numbers.
"""
from __future__ import annotations

import io
import re
from collections import defaultdict

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from s33weather.models import sha256

from ..db import utcnow
from ..models import Farm, MonthlyStat, RegionalStat, Workbook
from .base import Collector, Result, add_event, log_fetch

PAGE = "https://www.eirgrid.ie/grid/system-and-renewable-data-reports"
LINK_RE = re.compile(r'href="([^"]*DD-Summary-Report-V(\d+)\.xlsx)"', re.I)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LICENCE = "EirGrid Group - published for public information (System and Renewable Data Reports)"

# 'All RES DD Monthly Detailed': block start column (0-based) per jurisdiction; each tech has 4 columns
# (Availability, Generation, Dispatch Down MWh, Dispatch Down %) in this order.
RES_BLOCKS = {"IE": 2, "NI": 30, "AI": 58}
RES_TECHS = ["wind", "solar", "renewable_waste", "hydro", "other_res", "total_res"]
RES_TECH_LABELS = ["Wind", "Solar", "Renewable Waste", "Hydro", "Other RES", "Total RES"]
RES_METRICS = [("availability_mwh", "MWh"), ("generation_mwh", "MWh"), ("dd_mwh", "MWh"), ("dd_pct", "%")]

# 'Wind & Solar Monthly Detailed': wind cause breakdown, volumes (MWh) block per jurisdiction.
CAUSE_BLOCKS = {"AI": 2, "IE": 27, "NI": 52}
CAUSES = ["availability_mwh", "generation_mwh", "dd_mwh", "constraint_mwh", "transmission_constraint_mwh", "tso_testing_mwh",
          "curtailment_mwh", "highfreq_mingen_mwh", "rocof_inertia_mwh", "snsp_mwh", "other_reductions_mwh"]


def _cell(r: int, c: int) -> str:
    return f"{get_column_letter(c + 1)}{r + 1}"


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def parse_res_monthly(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    hdr = rows[2]
    for j, base in RES_BLOCKS.items():
        for k, label in enumerate(RES_TECH_LABELS):
            got = str(hdr[base + 4 * k] or "").strip()
            if label.lower() not in got.lower():
                raise ValueError(f"layout changed: expected {label!r} at {_cell(2, base + 4 * k)}, found {got!r}")
    out, year = [], None
    for ri, r in enumerate(rows[4:], start=4):
        if isinstance(r[0], int):
            year = r[0]
        if year is None or r[1] not in MONTHS:
            continue
        period = f"{year}-{MONTHS.index(r[1]) + 1:02d}"
        for j, base in RES_BLOCKS.items():
            for k, tech in enumerate(RES_TECHS):
                for m, (metric, unit) in enumerate(RES_METRICS):
                    c = base + 4 * k + m
                    v = _num(r[c])
                    if v is None:
                        continue
                    out.append(dict(jurisdiction=j, tech=tech, metric=metric, period=period, unit=unit,
                                    value=v * 100 if unit == "%" else v, sheet=ws.title, cell=_cell(ri, c)))
    return out


def parse_wind_causes(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    if "wind" not in str(rows[2][0] or "").lower() or "snsp" not in str(rows[3][11] or "").lower():
        raise ValueError("layout changed in 'Wind & Solar Monthly Detailed'")
    out, year = [], None
    for ri, r in enumerate(rows[4:], start=4):
        if isinstance(r[0], int):
            year = r[0]
        if year is None or r[1] not in MONTHS:
            continue
        period = f"{year}-{MONTHS.index(r[1]) + 1:02d}"
        for j, base in CAUSE_BLOCKS.items():
            for k, metric in enumerate(CAUSES):
                if metric in ("availability_mwh", "generation_mwh", "dd_mwh"):
                    continue  # already taken from the RES sheet
                v = _num(r[base + k])
                if v is not None:
                    out.append(dict(jurisdiction=j, tech="wind", metric=metric, period=period, unit="MWh", value=v,
                                    sheet=ws.title, cell=_cell(ri, base + k)))
    return out


def parse_regional(ws) -> tuple[list[dict], list[dict]]:
    rows = list(ws.iter_rows(values_only=True))
    # Row 3 (index 2) holds, per year block, Qtr1..Qtr4 (and from 2021 also Jan..Dec) and then
    # the year itself in the block's closing annual column. The year labels on row 2 are not
    # reliable ("2021 (Upgraded to monthly reporting)"), so each block is dated by its closing column.
    periods: dict[int, str] = {}
    pending: list[tuple[int, str]] = []
    for c, p in enumerate(rows[2]):
        if isinstance(p, str) and (p.startswith("Qtr") or p in MONTHS):
            pending.append((c, p))
        elif isinstance(p, int) and 2000 < p < 2100:
            for pc, lab in pending:
                periods[pc] = f"{p}Q{lab[3:]}" if lab.startswith("Qtr") else f"{p}-{MONTHS.index(lab) + 1:02d}"
            periods[c] = str(p)
            pending = []
        elif p == "Technology":
            break  # the farm list starts here
    first = str(rows[1][0] or "").lower()
    stats, tech, region = [], ("wind" if "controllable wind" in first else None), None
    for ri, r in enumerate(rows[3:], start=3):
        label = r[0]
        if isinstance(label, str):
            low = label.lower()
            if "controllable wind" in low and "region" in low:
                tech = "wind"
                continue
            if "controllable solar" in low and "region" in low:
                tech = "solar"
                continue
            if "totals" in low:
                tech = None  # totals blocks duplicate the jurisdiction figures we already hold
                continue
            if tech and label in ("MID", "NE", "NW", "SE", "SW", "W", "NI"):
                region, metric = label, "dd_pct"
            elif tech and region and label in ("Constraints", "Curtailments"):
                metric = "constraint_pct" if label == "Constraints" else "curtailment_pct"
            else:
                continue
            for c, period in periods.items():
                v = _num(r[c])
                if v is not None:
                    stats.append(dict(tech=tech, region=region, metric=metric, period=period, value=v * 100, sheet=ws.title, cell=_cell(ri, c)))
    # farm list
    hdr = rows[2]
    try:
        c0 = list(hdr).index("Technology")
    except ValueError:
        raise ValueError("layout changed: farm list header 'Technology' not found in 'Regional Wind & Solar'")
    farms, cur = [], [None, None, None]
    for r in rows[3:]:
        t = (list(r[c0:c0 + 6]) + [None] * 6)[:6]  # short rows are padded, not an error
        for i in range(3):
            if t[i]:
                cur[i] = t[i]
        if not t[4] or _num(t[5]) is None:
            continue
        farms.append(dict(tech=str(cur[0]).lower(), jurisdiction=str(cur[1]), region=str(cur[2]), node=t[3] and str(t[3]),
                          name=str(t[4]).strip(), capacity_mw=float(t[5])))
    return stats, farms


class EirGridDispatchDown(Collector):
    key = "eirgrid_dd"
    name = "EirGrid dispatch-down workbook (monthly)"
    publisher = "EirGrid Group"
    url = PAGE
    licence = LICENCE
    provides = "Monthly wind/solar dispatch-down (curtailment + constraints) for IE, NI and all-island since 2016; causes; regional rates; farm list with capacity."
    interval_hours = 6.0
    used_by = ("gridwatch",)

    def run(self, session: Session, client) -> Result:
        page = client.get(PAGE)
        page.raise_for_status()
        log_fetch(session, self.key, PAGE, sha256(page.content), len(page.content), self.licence, note="report index page")
        links = LINK_RE.findall(page.text)
        if not links:
            raise ValueError("no DD-Summary-Report link found on the EirGrid page (page layout changed?)")
        href, ver = max(links, key=lambda x: int(x[1]))
        url = href if href.startswith("http") else f"https://www.eirgrid.ie{href}"
        filename = url.rsplit("/", 1)[-1]
        if session.scalar(select(Workbook.id).where(Workbook.filename == filename)):
            return Result(message=f"{filename} already read")
        r = client.get(url)
        r.raise_for_status()
        digest = sha256(r.content)
        log_fetch(session, self.key, url, digest, len(r.content), self.licence, note=filename)
        wb = load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
        res_rows = parse_res_monthly(wb["All RES DD Monthly Detailed"])
        cause_rows = parse_wind_causes(wb["Wind & Solar Monthly Detailed"])
        reg_rows, farms = parse_regional(wb["Regional Wind & Solar"])
        if not res_rows:
            raise ValueError(f"{filename}: no monthly figures parsed")
        prev_latest = session.scalar(select(Workbook.latest_period).order_by(Workbook.retrieved_at.desc()).limit(1))
        latest = max(x["period"] for x in res_rows if x["jurisdiction"] == "IE" and x["tech"] == "wind" and x["metric"] == "dd_mwh")
        book = Workbook(kind="dd_summary", filename=filename, url=url, sha256=digest, bytes=len(r.content), latest_period=latest)
        session.add(book)
        session.flush()
        res = Result(fetched=len(res_rows) + len(cause_rows) + len(reg_rows) + len(farms))
        existing = {(m.jurisdiction, m.tech, m.metric, m.period): m for m in session.scalars(select(MonthlyStat))}
        for x in res_rows + cause_rows:
            k = (x["jurisdiction"], x["tech"], x["metric"], x["period"])
            cur = existing.get(k)
            if cur is None:
                session.add(MonthlyStat(workbook_id=book.id, **x))
                res.created += 1
            elif abs(cur.value - x["value"]) > 1e-6:  # EirGrid revises earlier months; keep the newest, traceably
                cur.value, cur.workbook_id, cur.sheet, cur.cell = x["value"], book.id, x["sheet"], x["cell"]
                res.changed += 1
        rexist = {(m.tech, m.region, m.metric, m.period): m for m in session.scalars(select(RegionalStat))}
        for x in reg_rows:
            cur = rexist.get((x["tech"], x["region"], x["metric"], x["period"]))
            if cur is None:
                session.add(RegionalStat(workbook_id=book.id, **x))
            elif abs(cur.value - x["value"]) > 1e-6:
                cur.value, cur.workbook_id, cur.sheet, cur.cell = x["value"], book.id, x["sheet"], x["cell"]
        self._farms(session, farms, book)
        if latest != prev_latest:
            self._month_event(session, latest, url, filename)
        res.message = f"{filename}: data to {latest}; {res.created} new, {res.changed} revised figures; {len(farms)} farms"
        return res

    def _farms(self, session: Session, farms: list[dict], book: Workbook) -> None:
        now = utcnow()
        before = defaultdict(float)
        for f in session.scalars(select(Farm)):
            before[(f.tech, f.region)] += f.capacity_mw
        exist = {(f.tech, f.name, f.node): f for f in session.scalars(select(Farm))}
        seen = set()
        for x in farms:
            k = (x["tech"], x["name"], x["node"])
            seen.add(k)
            f = exist.get(k)
            if f is None:
                session.add(Farm(workbook_id=book.id, first_seen=now, last_seen=now, **x))
            else:
                f.capacity_mw, f.region, f.jurisdiction, f.workbook_id, f.last_seen = x["capacity_mw"], x["region"], x["jurisdiction"], book.id, now
        for k, f in exist.items():
            if k not in seen:
                session.delete(f)
        session.flush()
        if before:
            after = defaultdict(float)
            for x in farms:
                after[(x["tech"], x["region"])] += x["capacity_mw"]
            for key in sorted(set(before) | set(after)):
                d = after[key] - before[key]
                if abs(d) >= 5:
                    add_event(session, "capacity_change", f"Controllable {key[0]} capacity in {key[1]} {'up' if d > 0 else 'down'} {abs(d):,.0f} MW to {after[key]:,.0f} MW ({book.filename})",
                              key=f"cap:{book.filename}:{key[0]}:{key[1]}", source_url=book.url)

    def _month_event(self, session: Session, period: str, url: str, filename: str) -> None:
        q = {m.metric: m.value for m in session.scalars(select(MonthlyStat).where(MonthlyStat.jurisdiction == "IE", MonthlyStat.tech == "wind",
                                                                                  MonthlyStat.period == period))}
        if "dd_pct" not in q:
            return
        y, mth = period.split("-")
        add_event(session, "dd_month",
                  f"{MONTHS[int(mth) - 1]} {y}: {q['dd_pct']:.1f}% of Ireland's available wind was dispatched down ({q.get('dd_mwh', 0) / 1000:,.0f} GWh)",
                  key=f"dd_month:{period}", detail=f"Source: {filename}", source_url=url)
