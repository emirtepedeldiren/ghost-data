"""Screenshot extraction (D2, D17): one cached LLM call per PNG, validated, then turned into Observations.

Every extracted id is checked against the expected pattern; values that do not match are kept in the
extraction log as `unparsed` and never used (never guessed).
"""
from __future__ import annotations

import re
from pathlib import Path

from audit import vocab
from audit.identity import EMAIL_RE, norm_email, norm_name, norm_phone, norm_user

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "vision_extract.md"
USER_ID_RE = re.compile(r"^user_\d+$")
VISION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "application": {"type": "STRING"},
        "page_title": {"type": "STRING"},
        "records": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "resource_id": {"type": "STRING"},
                    "user_id": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "email": {"type": "STRING"},
                    "phone": {"type": "STRING"},
                    "status": {"type": "STRING"},
                    "attributes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                        "key": {"type": "STRING"}, "value": {"type": "STRING"}}, "required": ["key", "value"]}},
                },
                "required": ["resource_id", "user_id", "name", "email", "phone", "status", "attributes"],
            },
        },
    },
    "required": ["application", "page_title", "records"],
}


def extract(client, path: Path) -> dict:
    res = client.generate("vision", "Transcribe the customer records in this screenshot.",
                          system=PROMPT_PATH.read_text(encoding="utf-8"), images=[path], json_schema=VISION_SCHEMA,
                          temperature=0.0)
    data = res.json()
    data["_model"] = res.model
    data["_cached"] = res.cached
    return data


def validate_record(r: dict) -> tuple[dict, list[str]]:
    """-> (clean record, problems). Invalid ids are dropped from the clean record and reported."""
    problems, clean = [], dict(r)
    rid = (r.get("resource_id") or "").strip()
    if rid and not vocab.RESOURCE_RE.fullmatch(rid):
        problems.append(f"unparsed resource_id {rid!r}")
        rid = ""
    clean["resource_id"] = rid
    uid = (r.get("user_id") or "").strip()
    if uid and not USER_ID_RE.match(uid):
        problems.append(f"unparsed user_id {uid!r}")
        uid = ""
    clean["user_id"] = uid
    email = (r.get("email") or "").strip()
    if email and not EMAIL_RE.fullmatch(email):
        problems.append(f"unparsed email {email!r}")
        email = ""
    clean["email"] = email
    return clean, problems


def record_identifiers(r: dict, unique_names: frozenset[str]) -> set[str]:
    out = set()
    if r.get("email"):
        out.add(norm_email(r["email"]))
    if r.get("user_id"):
        out.add(norm_user(r["user_id"]))
    if r.get("phone") and sum(c.isdigit() for c in r["phone"]) >= 8:
        out.add(norm_phone(r["phone"]))
    if r.get("name") and norm_name(r["name"]) in unique_names:
        out.add(norm_name(r["name"]))
    return out


def record_state(rid: str, r: dict, identifiers: set[str]) -> str | None:
    prefix = vocab.resource_prefix(rid)
    text = " ".join([r.get("status") or ""] + [f"{a.get('key')} {a.get('value')}" for a in r.get("attributes") or []]).lower()
    if prefix == "data_flow":
        return vocab.FLOW_DISABLED if re.search(r"\b(disabled|paused|stopped|inactive|off)\b", text) else vocab.FLOW_ACTIVE
    if prefix == "backup":
        if re.search(r"restore[^.]{0,20}(blocked|denied|disabled|not allowed)", text):
            return vocab.RESTORE_BLOCKED
        if re.search(r"restore[^.]{0,20}(allowed|enabled|eligible)", text):
            return vocab.RESTORE_ALLOWED
        return None   # a backup listing alone does not show restore access
    if prefix in ("audit_record", "legal_hold"):
        return vocab.RETAINED
    if prefix in ("tombstone", "analytics"):
        direct = {i for i in identifiers if not i.startswith("user:")}
        return vocab.PRESENT if direct else (vocab.REDACTED if prefix == "tombstone" else vocab.ANONYMIZED)
    return vocab.PRESENT
