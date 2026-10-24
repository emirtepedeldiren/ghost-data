"""Helpers shared by all evidence parsers: identifier scanning and structured-record state rules."""
from __future__ import annotations

import re

from audit import vocab
from audit.identity import extract_identifiers, norm_name

NAME_KEY = re.compile(r"(^|_)name$")
IDENTIFYING_KEYS = ("email", "phone", "email_hash", "user_id", "customer_reference",
                    "full_name", "name", "display_name", "requester_name", "requester_email",
                    "requester_phone", "owner_email", "owner_phone", "customer_email")


def is_null(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none"))


def truthy(v) -> bool:
    return v is True or (isinstance(v, (int, float)) and v == 1) or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes"))


def unit_identifiers(obj, unique_names: frozenset[str]) -> set[str]:
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


def resource_ids_in(obj) -> set[str]:
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


def has_direct_pii(rec: dict, keys) -> bool:
    return any(not is_null(rec.get(k)) for k in keys)


def text_has_pii(text) -> bool:
    if is_null(text):
        return False
    return any(i.split(":", 1)[0] in ("email", "phone") for i in extract_identifiers(str(text)))


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
