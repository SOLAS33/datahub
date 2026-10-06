"""One way to normalise a company or supplier name, shared by every Solas33 site and the hub's joins.

"Sisk Ltd." and "SISK LIMITED" become the same key; "Alpha Build" and "Alpha Builders" do not. The key is for matching text to text. It is not
an identity check: two different companies can share a key, and one company can appear under several.
"""
from __future__ import annotations

import re

LEGAL = re.compile(r"\b(ltd|limited|plc|dac|uc|unlimited company|llp|lp|inc|incorporated|co|company|teo|cpt|clg|t/a|trading as)\b\.?")
# Names that are not companies on the register: a match to a company of similar name would be wrong.
NOT_A_COMPANY = re.compile(r"(?<![a-z])(llp|lp|partnership|trading as|t/a|sole trader|& sons?|and sons?)(?![a-z])", re.I)


def norm_name(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s/]", " ", s)
    s = LEGAL.sub(" ", s)
    s = re.sub(r"\b(the|of|and)\b", " ", s) if len(s.split()) > 2 else s
    return re.sub(r"\s+", " ", s).strip()


def clean_name(s: str) -> str:
    """Drop the internal id some sources append ("An Garda Siochana_1192")."""
    return re.sub(r"_\d{2,}$", "", (s or "").strip())


def search_key(s: str) -> str:
    """Lower-case letters and digits only: what a person types, compared with what is on the register."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())
