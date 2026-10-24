"""State at audit_at (CONTRACT.md §6), category resolution (D5) and policy -> steps (§7.2).

State and citations come only from evidence files captured <= audit_at (D11 revised, D4).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from audit import vocab
from audit.model import Coverage, EvidenceIndex, Observation


@dataclass
class ResourceState:
    resource_id: str
    system_id: str
    state: str | None
    evidence_id: str | None
    source: str                     # "observation" | "coverage" | "none"
    category: str | None
    category_source: str
    action: str | None              # DELETE / REDACT / ANONYMIZE / RETAIN / DISABLE_FLOW / BLOCK_RESTORE
    notes: list[str] = field(default_factory=list)
    linked_by_evidence: bool = False   # set by the caller: at least one link came from an evidence file


class StateIndex:
    def __init__(self, index: EvidenceIndex, activity: list[dict], rules: list[dict]):
        self.obs_by_rid: dict[str, list[Observation]] = defaultdict(list)
        for o in index.observations:
            self.obs_by_rid[o.resource_id].append(o)
        self.coverage: list[Coverage] = index.coverage
        self.worker_by_rid: dict[str, list[dict]] = defaultdict(list)
        for a in activity:
            if a["source_system"] == "deletion_worker":
                self.worker_by_rid[a["resource_id"]].append(a)
        self.rule_action = {r["data_category"]: r["required_action"] for r in rules}
        self._captured = {o.file_id: o.captured_at for o in index.observations}
        self._captured.update({c.file_id: c.captured_at for c in index.coverage})

    # D5: explicit category -> worker's attempted operation -> prefix default.
    def category(self, rid: str, audit_at: str) -> tuple[str | None, str, str | None]:
        explicit = [o for o in self.obs_by_rid.get(rid, []) if o.category and o.captured_at <= audit_at]
        if not explicit:
            explicit = [o for o in self.obs_by_rid.get(rid, []) if o.category]
        if explicit:
            latest = max(explicit, key=lambda o: (o.captured_at, o.line_time or "", o.file_id))
            return latest.category, f"explicit:{latest.file_id}", None
        attempts = [a for a in self.worker_by_rid.get(rid, [])
                    if a["event_time"] <= audit_at and a["event_type"] in ("DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD")]
        if attempts:
            a = max(attempts, key=lambda a: a["event_time"])
            action = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE"}[a["event_type"]]
            return None, f"worker_hint:{a['activity_id']}", action
        prefix = vocab.resource_prefix(rid)
        return vocab.PREFIX_CATEGORY.get(prefix), "prefix_default", None

    def action_for(self, category: str | None, hint_action: str | None) -> str | None:
        if hint_action:
            return hint_action
        if category == "DATA_FLOW":
            return "DISABLE_FLOW"
        if category == "BACKUP_ARCHIVE":
            return "BLOCK_RESTORE"
        return self.rule_action.get(category or "")

    def resolve(self, rid: str, audit_at: str, absence_rule: str = "override") -> ResourceState:
        """State at audit_at. absence_rule decides when omission from a snapshot proves ABSENT:
          override  baseline: the latest snapshot omitting the resource beats an older explicit observation
          scope     (a) omission counts only within the snapshot's own store/collection scope
                    (a primary-store snapshot cannot clear a replica copy)
          fallback  (b) omission counts only when there is no explicit observation <= audit_at at all
          fallback_delete  (c) as (b), and only for resources whose policy is DELETE: omission from a partial
                    snapshot can show that a record was deleted, never that a retained/redacted record is gone
        """
        category, cat_src, hint = self.category(rid, audit_at)
        action = self.action_for(category, hint)
        all_obs = self.obs_by_rid.get(rid, [])
        named = [o.system_id for o in all_obs]
        system = max(sorted(set(named)), key=named.count) if named else vocab.system_for(rid)
        obs = [o for o in all_obs if o.observed_state and o.captured_at <= audit_at]
        cov_cat = category or vocab.PREFIX_CATEGORY.get(vocab.resource_prefix(rid) or "")
        absent_cov = [c for c in self.coverage
                      if c.captured_at <= audit_at and c.system_id == system and c.category == cov_cat
                      and rid not in c.present]
        latest = lambda xs: max(xs, key=lambda o: (o.captured_at, getattr(o, "line_time", None) or "", o.file_id)) if xs else None  # noqa: E731

        def from_obs(o, note=""):
            return ResourceState(rid, system, o.observed_state, o.file_id, "observation", category, cat_src, action,
                                 [f"{o.locator} ({o.unit_kind}){note}"])

        def from_cov(c, note=""):
            return ResourceState(rid, system, vocab.ABSENT, c.file_id, "coverage", category, cat_src, action,
                                 [f"absent from {c.locator}{note}"])

        if absence_rule in ("fallback", "fallback_delete"):
            if obs:
                return from_obs(latest(obs))
            if absent_cov and (absence_rule == "fallback" or action == "DELETE"):
                return from_cov(latest(absent_cov))
        elif absence_rule == "scope":
            per_store = []
            for store in sorted({o.store for o in obs} | {c.store for c in absent_cov}):
                o = latest([x for x in obs if x.store == store])
                c = latest([x for x in absent_cov if x.store == store])
                if o and (c is None or o.captured_at >= c.captured_at):
                    per_store.append(from_obs(o, f" [store={store or 'primary'}]"))
                elif c:
                    per_store.append(from_cov(c, f" [store={store or 'primary'}]"))
            if per_store:
                # Any store that still violates the policy decides the resource state (a surviving copy is a remaining record).
                violating = [r for r in per_store if STEP_TABLE.get((action, r.state), (None,))[0]]
                pool = violating or per_store
                return max(pool, key=lambda r: (r.evidence_id and self._captured.get(r.evidence_id, ""), r.evidence_id or ""))
        else:  # override (baseline)
            best, cov = latest(obs), latest(absent_cov)
            if best and (cov is None or best.captured_at >= cov.captured_at):
                return from_obs(best)
            if cov:
                return from_cov(cov)
        return ResourceState(rid, system, None, None, "none", category, cat_src, action, ["no state observation <= audit_at"])


# (action, state) -> (finding, remediation, verification); None = no step of that phase.
STEP_TABLE = {
    ("DELETE", vocab.PRESENT): ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    ("DELETE", vocab.ANONYMIZED): ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    ("DELETE", vocab.REDACTED): ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    ("DELETE", vocab.ABSENT): (None, None, "VERIFY_RECORD_ABSENT"),
    ("REDACT", vocab.PRESENT): ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    ("REDACT", vocab.REDACTED): (None, None, "VERIFY_RECORD_REDACTED"),
    ("REDACT", vocab.ABSENT): (None, None, "VERIFY_RECORD_ABSENT"),
    ("ANONYMIZE", vocab.PRESENT): ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    ("ANONYMIZE", vocab.ANONYMIZED): (None, None, "VERIFY_RECORD_ANONYMIZED"),
    ("ANONYMIZE", vocab.ABSENT): (None, None, "VERIFY_RECORD_ABSENT"),
    ("RETAIN", vocab.RETAINED): (None, None, "VERIFY_RECORD_PRESENT"),
    ("RETAIN", vocab.PRESENT): (None, None, "VERIFY_RECORD_PRESENT"),
    ("DISABLE_FLOW", vocab.FLOW_ACTIVE): ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    ("DISABLE_FLOW", vocab.FLOW_DISABLED): (None, None, "VERIFY_DATA_FLOW_DISABLED"),
    ("BLOCK_RESTORE", vocab.RESTORE_ALLOWED): ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
    ("BLOCK_RESTORE", vocab.RESTORE_BLOCKED): (None, None, "VERIFY_RESTORE_BLOCKED"),
}


# Required final state per action, used for verification steps that need no evidence (fix 2).
FINAL_VERIFICATION = {
    "DELETE": "VERIFY_RECORD_ABSENT", "REDACT": "VERIFY_RECORD_REDACTED", "ANONYMIZE": "VERIFY_RECORD_ANONYMIZED",
    "RETAIN": "VERIFY_RECORD_PRESENT", "DISABLE_FLOW": "VERIFY_DATA_FLOW_DISABLED", "BLOCK_RESTORE": "VERIFY_RESTORE_BLOCKED",
}


def build_steps(states: list[ResourceState], verify_unobserved: bool = False) -> tuple[str, list[dict], list[str]]:
    """-> (deletion_status, steps sorted per contract, notes about skipped resources).

    verify_unobserved (fix 2): in an INCOMPLETE case, a linked resource with no state observation still gets
    the verification of its required final state. These are future checks, so they need no evidence. In a
    COMPLETE case every verification must cite evidence, so unobserved resources are skipped.
    """
    from contract import OPERATION_PHASE, OPERATION_STATE, step_sort_key

    findings, remediation, verification, notes = [], [], [], []
    for rs in states:
        ops = STEP_TABLE.get((rs.action, rs.state))
        if ops is None:
            notes.append(f"{rs.resource_id}: no step for action={rs.action} state={rs.state} ({rs.source})")
            continue
        f, r, v = ops
        if f:
            findings.append((f, rs, rs.evidence_id or ""))
        if r:
            remediation.append((r, rs, ""))
        verification.append((v, rs, None))

    status = "INCOMPLETE" if findings else "COMPLETE"
    if verify_unobserved and status == "INCOMPLETE":
        for rs in states:
            # D11/D14: activity-only proposals (catalog, timing) without any observation get no step.
            if rs.state is None and rs.action in FINAL_VERIFICATION and rs.linked_by_evidence:
                verification.append((FINAL_VERIFICATION[rs.action], rs, None))
                notes.append(f"{rs.resource_id}: verification added without observation (fix 2)")
    steps = []
    for op, rs, ev in findings + remediation:
        steps.append({"phase": OPERATION_PHASE[op], "system_id": rs.system_id, "resource_id": rs.resource_id,
                      "operation": op, "state": OPERATION_STATE[op], "evidence_id": ev})
    for op, rs, _ in verification:
        ev = (rs.evidence_id or "") if status == "COMPLETE" else ""
        steps.append({"phase": OPERATION_PHASE[op], "system_id": rs.system_id, "resource_id": rs.resource_id,
                      "operation": op, "state": OPERATION_STATE[op], "evidence_id": ev})
    steps.sort(key=step_sort_key)
    for i, s in enumerate(steps, 1):
        s["step_no"] = i
    return status, steps, notes
