"""Which resources belong to the subject (CONTRACT.md §8, D11 revised, D12, D15).

Link sources:
  identifier            subject identifier in the same evidence unit, file captured <= audit_at
  identifier_post_audit same, in a file captured after audit_at (D12: ownership only; the unit's own
                        timestamp is <= audit_at or absent)
  identifier_post_audit_unit
                        same, but the unit's own timestamp is also after audit_at (D21: ownership only; the
                        resource gets a step only from a state observation <= audit_at, never a fix-2 verification)
  catalog               LINK_RESOURCE_IDENTITY activity naming a subject identifier (an activity *proposal*,
                        D11 revised). Vetoed only when an observation's identity resolves to a different known
                        user (D13 revised); an identity that resolves to no one is flagged, not vetoed
  timing                D20 (L2): deletion_worker schedule relative to reported_at, global unique assignment
Resources co-occurring in a unit (e.g. search_documents.source_resource_id) are linked only when the
unit carries no other person's identifiers.

Decisions: D11, D12, D13 (revised), D14, D20 (rule L2), D21; no tie-breaks between interchangeable candidates (D29, D32).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from audit import vocab
from audit.identity import IdentityResolver, Subject, extract_identifiers
from audit.model import Observation


@dataclass(frozen=True)
class Link:
    resource_id: str
    via: str
    source_id: str      # file_id or activity_id
    locator: str


class OwnershipIndex:
    """Global view: which users each resource's observations point to (time-independent)."""

    def __init__(self, observations: list[Observation], resolver: IdentityResolver):
        self.resolver = resolver
        self.owners: dict[str, set[str]] = defaultdict(set)
        for o in observations:
            users = resolver.owners_of(o.identifiers_present)
            if len(users) == 1:
                self.owners[o.resource_id] |= users


def explicit_links(subject: Subject, audit_at: str, observations: list[Observation],
                   resolver: IdentityResolver, post_audit_links: bool, post_audit_unit_links: bool = True) -> list[Link]:
    links: list[Link] = []
    for o in observations:
        hit = o.identifiers_present & subject.identifiers
        if not hit:
            continue
        if o.captured_at <= audit_at:
            via = "identifier"
        elif post_audit_links and (o.line_time is None or o.line_time <= audit_at):
            via = "identifier_post_audit"
        elif post_audit_links and post_audit_unit_links:
            via = "identifier_post_audit_unit"
        else:
            continue
        links.append(Link(o.resource_id, via, o.file_id, o.locator))
        others = resolver.owners_of(o.identifiers_present) - {subject.user_id}
        if not others:
            for rid in sorted(o.linked_resources):
                links.append(Link(rid, via + "+cooccurrence", o.file_id, o.locator))
    return links


def catalog_links(subject: Subject, audit_at: str, activity: list[dict], ownership: OwnershipIndex,
                  obs_by_rid: dict[str, list[Observation]] | None = None,
                  strict_veto: bool = False) -> tuple[list[Link], list[str], list[str]]:
    """-> (links, rejected, flags). strict_veto restores the original D13: any foreign identity vetoes."""
    links, rejected, flags = [], [], []
    for a in activity:
        if a["event_type"] != "LINK_RESOURCE_IDENTITY" or a["event_time"] > audit_at:
            continue
        if not (extract_identifiers(a["details"]) & subject.identifiers):
            continue
        rid = a["resource_id"]
        owners = ownership.owners.get(rid, set())
        if owners and subject.user_id not in owners:
            rejected.append(f"{a['activity_id']} {rid}: observations point to {sorted(owners)}, not {subject.user_id}")
            continue
        # D13 revised: an observation vetoes the claim only when the identity it shows resolves to another known user.
        veto = flag = None
        for o in (obs_by_rid or {}).get(rid, []):
            direct = {i for i in o.identifiers_present if i.split(":", 1)[0] in ("email", "phone", "name")}
            if not direct or direct & subject.identifiers:
                continue
            others = ownership.resolver.owners_of(direct) - {subject.user_id}
            if others or strict_veto:
                veto = f"{a['activity_id']} {rid}: {o.file_id} shows {sorted(others) or sorted(direct)[:2]}"
                break
            flag = f"{a['activity_id']} {rid}: identity-mismatch, {o.file_id} shows {sorted(direct)[:2]} (resolves to no known user)"
        if veto:
            rejected.append(veto)
            continue
        if flag:
            flags.append(flag)
        links.append(Link(rid, "catalog", a["activity_id"], a["details"][:80]))
    return links, rejected, flags


# D20 (L2). Bounds are minutes relative to reported_at, calibrated label-free on eval identifier links
# (account deletes p1..p99 = -74.8..-44.0, policy follow-ups -29.0..-2.9); +10 covers worker reports after reported_at.
ACCOUNT_WINDOW = (-80, 10)
FOLLOWUP_WINDOW = (-35, 0)      # [lo, hi): follow-ups run before the workflow reports
RESULT_OK = {"COMPLETE": {"SUCCESS"}, "NOT_STARTED": {"FAILED"}, "PARTIALLY_COMPLETE": {"PARTIAL", "SUCCESS"}}
FOLLOWUP_ACTIONS = {"ANONYMIZE_RECORD", "REDACT_RECORD", "DELETE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "RETAIN_RECORD"}


def _minutes(a: str, b: str) -> float:
    from datetime import datetime
    p = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return (p(a) - p(b)).total_seconds() / 60


class TimingLinker:
    """D20 (L2): link deletion_worker events to requests by schedule, globally, only when unique.

    Pool: requests that authorize deletion and resolve to a subject (eval + sample request rows, never answers).
    Account DELETE events: event in ACCOUNT_WINDOW of reported_at, result compatible with reported_status,
      not observed to belong to another user, and the subject owns no account through identifier evidence.
    Follow-up actions on other resources: event in FOLLOWUP_WINDOW, no result filter, not observed to belong to
      another user, and the subject has no resource of that type yet (owned or assigned). A resource owned
      only by people without a request goes to nobody.
    Elimination repeats until stable; ambiguous events are logged with their candidates, never tie-broken.
    """

    def __init__(self, requests: list[dict], activity: list[dict], ownership: OwnershipIndex,
                 resolver: IdentityResolver, authorizes=lambda text: True):
        self.assigned: dict[str, list[Link]] = defaultdict(list)
        self.ambiguous: dict[str, list[str]] = defaultdict(list)
        self.uid: dict[str, str] = {}
        pool = []
        for r in requests:
            s, _ = resolver.resolve(r["email"])
            if s and authorizes(r["request_text"]):
                self.uid[r["case_id"]] = s.user_id
                pool.append(r)
        self.requesting = set(self.uid.values())
        self.owned_by: dict[str, set[str]] = defaultdict(set)
        for rid, users in ownership.owners.items():
            if len(users) == 1:
                self.owned_by[next(iter(users))].add(rid)
        self.ownership = ownership
        self.taken: dict[str, set[str]] = defaultdict(set)   # user -> resource ids assigned by timing
        worker = [a for a in activity if a["source_system"] == "deletion_worker"]
        accounts = [a for a in worker if a["event_type"] == "DELETE_RECORD" and vocab.resource_prefix(a["resource_id"]) == "account"]
        followups = [a for a in worker if a["event_type"] in FOLLOWUP_ACTIONS and vocab.resource_prefix(a["resource_id"]) != "account"]
        self._solve(pool, accounts, ACCOUNT_WINDOW, True, "account delete in request schedule")
        self._solve(pool, followups, FOLLOWUP_WINDOW, False, "worker follow-up in request schedule")

    def _slot_full(self, user: str, rid: str) -> bool:
        prefix = vocab.resource_prefix(rid)
        return any(vocab.resource_prefix(x) == prefix for x in self.owned_by[user] | self.taken[user])

    def _solve(self, pool, events, window, is_account, locator):
        lo, hi = window
        cands: list[tuple[dict, list[str]]] = []
        for a in events:
            rid = a["resource_id"]
            owners = self.ownership.owners.get(rid, set())
            if owners and not owners & self.requesting:
                continue  # belongs to someone without a deletion request
            cs = []
            for r in pool:
                u = self.uid[r["case_id"]]
                off = _minutes(a["event_time"], r["reported_at"])
                if not (lo <= off <= hi if is_account else lo <= off < hi):
                    continue
                if owners and u not in owners:
                    continue
                if is_account and a["reported_result"] not in RESULT_OK.get(r["reported_status"], set()):
                    continue
                cs.append(r["case_id"])
            if cs:
                cands.append((a, cs))
        done: set[str] = set()
        changed = True
        while changed:
            changed = False
            for a, cs in cands:
                rid = a["resource_id"]
                if a["activity_id"] in done or any(rid in t for t in self.taken.values()):
                    continue
                live = [c for c in cs if rid in self.owned_by[self.uid[c]] or not self._slot_full(self.uid[c], rid)]
                if len(live) == 1:
                    c = live[0]
                    self.assigned[c].append(Link(rid, "timing", a["activity_id"], locator))
                    self.taken[self.uid[c]].add(rid)
                    done.add(a["activity_id"])
                    changed = True
        for a, cs in cands:
            if a["activity_id"] in done or any(a["resource_id"] in t for t in self.taken.values()):
                continue
            live = [c for c in cs if a["resource_id"] in self.owned_by[self.uid[c]] or not self._slot_full(self.uid[c], a["resource_id"])]
            if len(live) > 1:
                for c in live:
                    self.ambiguous[c].append(f"{a['activity_id']} {a['resource_id']} candidates {live}")
