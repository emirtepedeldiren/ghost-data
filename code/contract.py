"""Output contract shared by the pipeline, the validator and the evaluator.

Single source of truth for the CONTRACT.md §3 rules: header, statuses, the allowed
(phase, operation, state) triples, their within-phase rank, and the step sort key.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = REPO_ROOT / "dataset"

OUTPUT_HEADER = ["case_id", "deletion_status", "steps_json"]
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")
STEP_KEYS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
STEP_FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")

# (phase, operation, state); list order within a phase is the rank used for sorting (problem_statement.md).
OPERATIONS = (
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
)
OPERATION_PHASE = {op: phase for phase, op, _ in OPERATIONS}
OPERATION_STATE = {op: state for _, op, state in OPERATIONS}
OPERATION_RANK: dict[str, int] = {}
for _phase in PHASES:
    for _rank, (_p, _op, _s) in enumerate(o for o in OPERATIONS if o[0] == _phase):
        OPERATION_RANK[_op] = _rank


def step_sort_key(step: dict) -> tuple:
    """D6: phase, then operation rank, then system_id, then resource_id."""
    return (
        PHASES.index(step["phase"]),
        OPERATION_RANK[step["operation"]],
        step["system_id"],
        step["resource_id"],
    )


_CASE_NUM = re.compile(r"^case_(\d+)$")


def case_sort_key(case_id: str) -> tuple:
    """D7: numeric case_id order (case_33 < case_100); non-conforming ids sort last."""
    m = _CASE_NUM.match(case_id)
    return (0, int(m.group(1)), case_id) if m else (1, 0, case_id)


def dumps_steps(steps: list[dict]) -> str:
    """Deterministic serialization with the fixed STEP_KEYS order."""
    return json.dumps(
        [{k: s[k] for k in STEP_KEYS} for s in steps],
        ensure_ascii=False,
        separators=(",", ":"),
    )
