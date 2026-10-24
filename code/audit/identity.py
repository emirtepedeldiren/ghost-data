"""Identity resolution and identifier extraction.

Identifiers are normalized to tagged strings so that values from any evidence format compare equal:
    email:<lowercase>   phone:<digits>   hash:<lowercase hex64>   user:<user_N>   name:<casefold>
Matching is exact on the normalized value; resource and user ids use token boundaries, so
support_864 never matches support_8642 and user_1 never matches user_13.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![A-Za-z0-9\-])")
USER_RE = re.compile(r"(?<![A-Za-z0-9_])user_\d+(?![0-9A-Za-z_])")
HASH_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
PHONE_RE = re.compile(r"\+\d[\d \-().]{6,}\d")


def norm_email(v: str) -> str:
    return "email:" + v.strip().lower()


def norm_phone(v: str) -> str:
    return "phone:" + "".join(c for c in v if c.isdigit())


def norm_hash(v: str) -> str:
    return "hash:" + v.strip().lower()


def norm_user(v: str) -> str:
    return "user:" + v.strip()


def norm_name(v: str) -> str:
    return "name:" + " ".join(v.split()).casefold()


def extract_identifiers(text: str, names: frozenset[str] = frozenset()) -> set[str]:
    """All identifiers in free text. Names are only recognized when they are known unique full names."""
    found: set[str] = set()
    for m in EMAIL_RE.finditer(text):
        found.add(norm_email(m.group(0)))
    for m in USER_RE.finditer(text):
        found.add(norm_user(m.group(0)))
    for m in HASH_RE.finditer(text):
        found.add(norm_hash(m.group(0)))
    for m in PHONE_RE.finditer(text):
        digits = norm_phone(m.group(0))
        if len(digits) - len("phone:") >= 8:
            found.add(digits)
    if names:
        folded = " ".join(text.split()).casefold()
        for n in names:
            # n is "name:<casefold full name>"; require word boundaries around the full name.
            raw = n[len("name:"):]
            if raw in folded and re.search(r"(?<!\w)" + re.escape(raw) + r"(?!\w)", folded):
                found.add(n)
    return found


def direct_identifiers(values) -> set[str]:
    """Identifiers present in a list of field values (None / 'null' / '' ignored)."""
    out: set[str] = set()
    for v in values:
        if v is None:
            continue
        s = str(v)
        if s.strip().lower() in ("", "null", "none", "-", "—"):
            continue
        out |= extract_identifiers(s)
    return out


@dataclass
class Subject:
    user_id: str
    request_email: str
    identifiers: frozenset[str]
    current: dict
    history: list[dict] = field(default_factory=list)
    resolution: str = ""          # "current" or "historical"


class IdentityResolver:
    def __init__(self, identities: list[dict], audit_log: list[dict]):
        self.identities = {r["user_id"]: r for r in identities}
        self.audit_by_user: dict[str, list[dict]] = {}
        for a in audit_log:
            self.audit_by_user.setdefault(a["user_id"], []).append(a)
        self.owner: dict[str, set[str]] = {}
        for uid, r in self.identities.items():
            for ident in self._row_identifiers(r):
                self.owner.setdefault(ident, set()).add(uid)
        for a in audit_log:
            for v in (a["old_value"], a["new_value"]):
                ident = self._audit_identifier(a["field_name"], v)
                if ident:
                    self.owner.setdefault(ident, set()).add(a["user_id"])
        name_counts: dict[str, int] = {}
        for r in identities:
            name_counts[norm_name(r["name"])] = name_counts.get(norm_name(r["name"]), 0) + 1
        self.unique_names = frozenset(n for n, c in name_counts.items() if c == 1)

    @staticmethod
    def _row_identifiers(r: dict) -> set[str]:
        out = {norm_user(r["user_id"])}
        if r.get("email"):
            out.add(norm_email(r["email"]))
        if r.get("phone"):
            out.add(norm_phone(r["phone"]))
        if r.get("email_hash"):
            out.add(norm_hash(r["email_hash"]))
        return out

    @staticmethod
    def _audit_identifier(field_name: str, value: str) -> str | None:
        if not value:
            return None
        return {"email": norm_email, "phone": norm_phone, "email_hash": norm_hash}.get(field_name, lambda _: None)(value)

    def resolve(self, request_email: str) -> tuple[Subject | None, str]:
        key = norm_email(request_email)
        owners = self.owner.get(key, set())
        if len(owners) != 1:
            return None, ("no identity matches the request email" if not owners
                          else f"request email maps to {len(owners)} users: {sorted(owners)}")
        uid = next(iter(owners))
        cur = self.identities.get(uid)
        if cur is None:
            return None, f"{uid} appears only in the audit log, not in user_identities.csv"
        history = sorted(self.audit_by_user.get(uid, []), key=lambda a: a["event_time"])
        idents = self._row_identifiers(cur)
        for a in history:
            for v in (a["old_value"], a["new_value"]):
                ident = self._audit_identifier(a["field_name"], v)
                if ident:
                    idents.add(ident)
        name = norm_name(cur["name"])
        if name in self.unique_names:
            idents.add(name)
        resolution = "current" if norm_email(cur["email"]) == key else "historical"
        return Subject(uid, request_email, frozenset(idents), cur, history, resolution), resolution

    def owners_of(self, identifiers) -> set[str]:
        """Users owning any of the given identifiers (names excluded: they are only a supporting signal)."""
        out: set[str] = set()
        for i in identifiers:
            if not i.startswith("name:"):
                out |= self.owner.get(i, set())
        return out
