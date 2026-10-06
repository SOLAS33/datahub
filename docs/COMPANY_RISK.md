# Company risk dataset (rules 1.0)

Built by the `cro_companies` collector (`s33hub/collectors/business.py`) with `s33hub/risk.py` every run (the rows are parsed each run because the flags depend on today's date; the publish ledger uploads only files whose bytes changed). Consumed by FirmWatch Ireland (https://firmwatch.solas33.com).

## Files (all under `v1/business/risk/`)

| File | Contents |
|---|---|
| `company/<n>.json.gz` | `{"v": "1.0", "c": {"<number>": record}}` for company numbers `n*1000` to `n*1000+999` |
| `search/index.json` | `{"split": [2-letter prefixes split by 3 letters], "rules": "1.0"}` |
| `search/<prefix>.json.gz` | `[[number, name, status_group, registration_year, level], …]` for names whose search key starts with the prefix |
| `sector_filing.csv` | per NACE division: live companies, overdue annual returns, stale accounts, late filers, registered in last 12 months, in insolvency, strike-off listed |
| `status_events_monthly.csv.gz` | month, status group, companies (by the month the current status began) |
| `strike_off_list.csv.gz` | every company currently listed for strike-off |
| `recent_insolvencies.csv.gz` | insolvency statuses begun in the last 12 months |
| `meta.json` | rules version, thresholds, counts |

A 2-letter search prefix whose file would exceed 5,000 names is split into 3-letter files and listed in `split`.

## Record keys

`n` number, `nm` name, `st` status, `sg` status group (L live, I insolvency, S strike-off, D dissolved or ceased), `ty` type, `rg` registered, `ds` dissolved, `sd` status date, `ar` last annual return, `ac` latest accounts to, `nc` NACE, `ea` Eircode routing key, `ne` name effective, `f` filings `[received, period_end, days_after]` (latest first), `fl` flags `[code, severity, text]`, `lv` level, `pc` public contracts `{w wins, v sole-award value, fy, ly}`.

## Rules

| Flag | Severity | Rule |
|---|---|---|
| INSOLVENCY | red | status group I |
| STRIKE_OFF | red | status group S |
| NOT_LIVE | grey | status group D |
| AR_OVERDUE / AR_NONE | amber | live, older than 18 months, last annual return > 16 months ago or none |
| ACCOUNTS_STALE / ACCOUNTS_NONE | amber | live, older than 30 months, latest accounts period end > 30 months ago or none |
| LATE_FILER | amber | live; last two accounts received > 300 days after period end |
| NEW | info | live, registered < 12 months ago |
| NEW_AND_LARGE | amber | live, < 12 months old, sole public awards ≥ €250,000 |
| NAME_CHANGED | info | live, name effective within 12 months |

Level: red if any red; else grey if any grey; else amber if any amber; else green. Changing a threshold means bumping `RULES_VERSION`.

## Public-contract join

Exact `norm_name` match (case, punctuation and legal-form words removed; `s33hub/names.py`), exactly one live company with that name, partnerships/LLPs/sole traders excluded (`NOT_A_COMPANY`). Winners come from the hub's published eTenders and TED files.

## Privacy

No directors or individuals; addresses reduced to the Eircode routing key at collection.

## Sanctions name match (rules 1.1)

`SANCTIONS_NAME` (amber): a live company whose normalised name equals a name or alias of an *enterprise* on the EU consolidated financial sanctions list (hub file `v1/governance/sanctions/eu_enterprises.csv.gz`), when exactly one live company has that name and the normalised name is at least 6 characters. This is a name match, not an identification: many matches are coincidences (on the first run, 3 of 326,000 live companies). The record carries `sx` `{id, n, p, d}` (entity id, listed name, programme, designation date). Individuals on the list are not republished.

## Planning pipeline

`v1/property/planning/monthly_authority.csv.gz` (applications, grants, refusals, homes and floor area per planning authority and month) and `major_applications.csv.gz` (at least 50 homes or 5,000 m2). The national planning dataset carries no applicant names, so planning applications cannot be linked to companies; no addresses are republished.
