"""Which resources belong to the subject (CONTRACT.md §8, D11 revised, D12, D15).

Link sources:
  identifier            subject identifier in the same evidence unit, file captured <= audit_at
  identifier_post_audit same, in a file captured after audit_at (D12: ownership only; the unit's own
                        timestamp must be <= audit_at when it has one)
  catalog               LINK_RESOURCE_IDENTITY activity naming a subject identifier (an activity
                        *proposal*, D11 revised); rejected when an observation of the resource shows
                        only another person's identifiers
  timing                deletion_worker activity in the request window, unique one-to-one assignment
                        (flag-controlled, D15)
Resources co-occurring in a unit (e.g. search_documents.source_resource_id) are linked only when the
unit carries no other person's identifiers.
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
                   resolver: IdentityResolver, post_audit_links: bool) -> list[Link]:
    links: list[Link] = []
    for o in observations:
        hit = o.identifiers_present & subject.identifiers
        if not hit:
            continue
        if o.captured_at <= audit_at:
            via = "identifier"
        elif post_audit_links and (o.line_time is None or o.line_time <= audit_at):
            via = "identifier_post_audit"
        else:
            continue
        links.append(Link(o.resource_id, via, o.file_id, o.locator))
        others = resolver.owners_of(o.identifiers_present) - {subject.user_id}
        if not others:
            for rid in sorted(o.linked_resources):
                links.append(Link(rid, via + "+cooccurrence", o.file_id, o.locator))
    return links


def catalog_links(subject: Subject, audit_at: str, activity: list[dict], ownership: OwnershipIndex) -> tuple[list[Link], list[str]]:
    links, rejected = [], []
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
        links.append(Link(rid, "catalog", a["activity_id"], a["details"][:80]))
    return links, rejected


STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}  # ASSUMPTION, fits 7/7 checked samples
WORKER_ACTIONS = {"DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE"}


class TimingLinker:
    """D3/D15: link deletion_worker activity to a request only when the assignment is unique.

    Step 1: account DELETE_RECORD events in [requested_at, reported_at] whose reported_result matches
            the request's reported_status, minus accounts observed to belong to someone else;
            iterate unique assignments across *all* requests until stable.
    Step 2: other worker actions in (assigned account event, reported_at] of exactly one assigned request.
    """

    def __init__(self, requests: list[dict], activity: list[dict], ownership: OwnershipIndex,
                 resolver: IdentityResolver):
        self.assigned: dict[str, list[Link]] = defaultdict(list)
        self.ambiguous: dict[str, list[str]] = defaultdict(list)
        worker = [a for a in activity if a["source_system"] == "deletion_worker" and a["event_type"] in WORKER_ACTIONS]
        accounts = [a for a in worker if a["event_type"] == "DELETE_RECORD" and vocab.resource_prefix(a["resource_id"]) == "account"]
        subj_of: dict[str, str | None] = {}
        for r in requests:
            s, _ = resolver.resolve(r["email"])
            subj_of[r["case_id"]] = s.user_id if s else None

        cands: dict[str, list[dict]] = {}
        for r in requests:
            want = STATUS_RESULT.get(r["reported_status"])
            uid = subj_of[r["case_id"]]
            cands[r["case_id"]] = [
                a for a in accounts
                if r["requested_at"] <= a["event_time"] <= r["reported_at"] and a["reported_result"] == want
                and not (ownership.owners.get(a["resource_id"]) and uid not in ownership.owners[a["resource_id"]])
            ]
        # Accounts observed to belong to the request's own subject are assigned first.
        account_event: dict[str, dict] = {}
        for r in requests:
            uid = subj_of[r["case_id"]]
            own = [a for a in cands[r["case_id"]] if uid and uid in ownership.owners.get(a["resource_id"], set())]
            if len(own) == 1:
                account_event[r["case_id"]] = own[0]
        changed = True
        while changed:
            changed = False
            taken = {e["activity_id"] for e in account_event.values()}
            for cid, cs in cands.items():
                if cid in account_event:
                    continue
                free = [a for a in cs if a["activity_id"] not in taken]
                if len(free) == 1:
                    account_event[cid] = free[0]
                    taken.add(free[0]["activity_id"])
                    changed = True
        for cid, cs in cands.items():
            if cid not in account_event and len(cs) > 1:
                self.ambiguous[cid].append(f"{len(cs)} candidate account events: {[a['resource_id'] for a in cs][:6]}")
        req_by_id = {r["case_id"]: r for r in requests}
        for cid, ev in account_event.items():
            self.assigned[cid].append(Link(ev["resource_id"], "timing", ev["activity_id"], "account delete in request window"))

        # Step 2: follow-up actions on other resources.
        windows = {cid: (ev["event_time"], req_by_id[cid]["reported_at"]) for cid, ev in account_event.items()}
        for a in worker:
            if vocab.resource_prefix(a["resource_id"]) == "account":
                continue
            owners_ok = lambda cid: not (ownership.owners.get(a["resource_id"]) and subj_of[cid] not in ownership.owners[a["resource_id"]])  # noqa: E731
            hits = [cid for cid, (lo, hi) in windows.items() if lo < a["event_time"] <= hi and owners_ok(cid)]
            if len(hits) == 1:
                self.assigned[hits[0]].append(Link(a["resource_id"], "timing", a["activity_id"], "worker action after assigned account delete"))
            elif len(hits) > 1:
                for cid in hits:
                    self.ambiguous[cid].append(f"{a['activity_id']} {a['resource_id']} shared by {len(hits)} request windows")
