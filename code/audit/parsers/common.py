"""Helpers shared by all evidence parsers: identifier scanning and structured-record state rules.

Decisions: record state rules of CONTRACT.md §6; D23 (parse_time, retention_end).
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from audit import vocab
from audit.identity import extract_identifiers, norm_name

NAME_KEY = re.compile(r"(^|_)name$")


def is_null(v: object) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none"))


def truthy(v: object) -> bool:
    return v is True or (isinstance(v, (int, float)) and v == 1) or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes"))


def unit_identifiers(obj: object, unique_names: frozenset[str]) -> set[str]:
    """Every identifier inside a (nested) record: emails/phones/hashes/user ids from any string value,
    plus unique full names from *name fields."""
    found: set[str] = set()

    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str) and not is_null(o):
            found.update(extract_identifiers(o))
            if NAME_KEY.search(key):
                n = norm_name(o)
                if n in unique_names:
                    found.add(n)

    walk(obj)
    return found


def resource_ids_in(obj: object) -> set[str]:
    out: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            out.update(m.group(0) for m in vocab.RESOURCE_RE.finditer(o))

    walk(obj)
    return out


def has_direct_pii(rec: dict, keys: Iterable[str]) -> bool:
    return any(not is_null(rec.get(k)) for k in keys)


def text_has_pii(text: object) -> bool:
    if is_null(text):
        return False
    return any(i.split(":", 1)[0] in ("email", "phone") for i in extract_identifiers(str(text)))


RETENTION_KEYS = ("retention_until", "retained_until")
_DMY = re.compile(r"^\s*(\d{1,2}) ([A-Za-z]{3}) (\d{4})(?:\s+(\d{2})\s*[:\s]\s*(\d{2}))?")


def parse_time(value: object) -> str | None:
    """ISO-8601 or '25 Aug 2027[ 04:03 UTC]' -> 'YYYY-MM-DDTHH:MM:SSZ'; None when not a date."""
    from datetime import datetime
    if is_null(value):
        return None
    v = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", v):
        return v
    m = _DMY.match(v)
    if not m:
        return None
    day, mon, year, hh, mm = m.groups()
    try:
        d = datetime.strptime(f"{day} {mon} {year}", "%d %b %Y")
    except ValueError:
        return None
    return d.strftime("%Y-%m-%d") + f"T{hh or '00'}:{mm or '00'}:00Z"


def retention_end(rec: dict) -> str | None:
    """Retention / legal-hold end date stated by a structured record (D23)."""
    for k in RETENTION_KEYS:
        t = parse_time(rec.get(k))
        if t:
            return t
    return None


def record_state(category: str | None, rec: dict) -> str | None:
    """Observed state of a structured record (JSON object / SQL row) that is present in a snapshot."""
    if category == "DATA_FLOW":
        return vocab.FLOW_ACTIVE if truthy(rec.get("enabled")) else vocab.FLOW_DISABLED
    if category == "BACKUP_ARCHIVE":
        return vocab.RESTORE_ALLOWED if truthy(rec.get("restore_allowed")) else vocab.RESTORE_BLOCKED
    if category in vocab.RETAINED_CATEGORIES:
        return vocab.RETAINED
    if category == "ANALYTICS_PROFILE":
        return vocab.PRESENT if has_direct_pii(rec, ("user_id", "customer_reference", "email", "phone", "email_hash")) else vocab.ANONYMIZED
    if category == "ACCOUNT_TOMBSTONE":
        return vocab.PRESENT if has_direct_pii(rec, ("name", "email", "phone", "email_hash")) else vocab.REDACTED
    if category == "SUPPORT_MESSAGE":
        if has_direct_pii(rec, ("requester_name", "requester_email", "requester_phone")):
            return vocab.PRESENT
        if text_has_pii(rec.get("message_text")) or text_has_pii(rec.get("message_excerpt")):
            return vocab.PRESENT
        return vocab.REDACTED
    # USER_PROFILE, SEARCH_INDEX_ENTRY, SUPPORT_ATTACHMENT, USER_EXPORT, ...: the record exists.
    return vocab.PRESENT
