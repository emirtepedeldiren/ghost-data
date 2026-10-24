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

    def resolve(self, rid: str, audit_at: str) -> ResourceState:
        category, cat_src, hint = self.category(rid, audit_at)
        action = self.action_for(category, hint)
        obs = [o for o in self.obs_by_rid.get(rid, []) if o.observed_state and o.captured_at <= audit_at]
        named = [o.system_id for o in self.obs_by_rid.get(rid, [])]
        system = max(set(named), key=named.count) if named else vocab.system_for(rid)
        best = max(obs, key=lambda o: (o.captured_at, o.line_time or "", o.file_id)) if obs else None

        cov_cat = category or vocab.PREFIX_CATEGORY.get(vocab.resource_prefix(rid) or "")
        absent_cov = [c for c in self.coverage
                      if c.captured_at <= audit_at and c.system_id == system and c.category == cov_cat
                      and rid not in c.present]
        cov = max(absent_cov, key=lambda c: (c.captured_at, c.file_id)) if absent_cov else None

        if best and (cov is None or best.captured_at >= cov.captured_at):
            return ResourceState(rid, system, best.observed_state, best.file_id, "observation", category, cat_src, action,
                                 [f"{best.locator} ({best.unit_kind})"])
        if cov:
            return ResourceState(rid, system, vocab.ABSENT, cov.file_id, "coverage", category, cat_src, action,
                                 [f"absent from {cov.locator}"])
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


def build_steps(states: list[ResourceState]) -> tuple[str, list[dict], list[str]]:
    """-> (deletion_status, steps sorted per contract, notes about skipped resources)."""
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
