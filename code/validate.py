"""Validate a predictions CSV against the output contract (CONTRACT.md §3, §11).

Usage:
    python3 code/validate.py [--output output.csv] [--requests dataset/deletion_requests.csv]

Exit code 0 when every check passes, 1 otherwise. Problems are grouped by check and
each group lists at most --max-examples offending cases.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import (
    DATASET_DIR,
    OPERATION_PHASE,
    OPERATION_STATE,
    OUTPUT_HEADER,
    PHASES,
    REPO_ROOT,
    STATUSES,
    STEP_KEYS,
    case_sort_key,
    step_sort_key,
)


def load_evidence_times(dataset_dir: Path) -> dict[str, str]:
    """Every citable id (file_id, activity_id, audit_id) -> its timestamp."""
    times: dict[str, str] = {}
    sources = (
        ("evidence_files.csv", "file_id", "captured_at"),
        ("system_activity.csv", "activity_id", "event_time"),
        ("user_audit_log.csv", "audit_id", "event_time"),
    )
    for name, id_col, time_col in sources:
        path = dataset_dir / name
        if path.exists():
            with path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    times[row[id_col]] = row[time_col]
    return times


def check_steps(case_id: str, status: str, steps: list, audit_at: str | None,
                evidence_times: dict[str, str], problems: dict[str, list[str]]) -> None:
    def bad(check: str, msg: str) -> None:
        problems[check].append(f"{case_id}: {msg}")

    if status == "NOT_APPLICABLE":
        if steps:
            bad("not_applicable_must_be_empty", f"NOT_APPLICABLE with {len(steps)} steps")
        return
    if status in ("COMPLETE", "INCOMPLETE") and not steps:
        # D27: not forbidden by problem_statement.md, but a COMPLETE without proof is a known limitation (NO_OBSERVABLE_RECORDS).
        problems["WARN:NO_OBSERVABLE_RECORDS"].append(f"{case_id}: {status} with no steps (no observable record <= audit_at, D27)")
        return

    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            bad("step_not_object", f"step {i} is {type(step).__name__}")
            return
        keys = set(step)
        if keys != set(STEP_KEYS):
            missing, extra = sorted(set(STEP_KEYS) - keys), sorted(keys - set(STEP_KEYS))
            bad("step_keys", f"step {i} missing={missing} extra={extra}")
            return
        if not isinstance(step["step_no"], int) or isinstance(step["step_no"], bool):
            bad("step_no_type", f"step {i} step_no={step['step_no']!r} is not an integer")
        elif step["step_no"] != i:
            bad("step_no_consecutive", f"position {i} has step_no={step['step_no']}")
        for k in STEP_KEYS[1:]:
            if not isinstance(step[k], str):
                bad("field_type", f"step {i} {k}={step[k]!r} is not a string")
        op = step["operation"]
        if op not in OPERATION_PHASE:
            bad("unknown_operation", f"step {i} operation={op!r}")
            continue
        if step["phase"] != OPERATION_PHASE[op] or step["state"] != OPERATION_STATE[op]:
            bad("phase_operation_state", f"step {i} ({step['phase']}, {op}, {step['state']}) is not an allowed triple")
        if not step["system_id"] or not step["resource_id"]:
            bad("empty_system_or_resource", f"step {i} system_id/resource_id empty")

        ev = step["evidence_id"]
        phase = step["phase"]
        if status == "COMPLETE":
            if phase != "VERIFICATION":
                bad("complete_only_verification", f"step {i} phase={phase}")
            if not ev:
                bad("complete_needs_evidence", f"step {i} {op} {step['resource_id']} has empty evidence_id")
        elif status == "INCOMPLETE":
            if phase == "FINDING" and not ev:
                bad("finding_needs_evidence", f"step {i} {op} {step['resource_id']} has empty evidence_id")
            if phase in ("REMEDIATION", "VERIFICATION") and ev:
                bad("incomplete_future_steps_empty_evidence", f"step {i} {phase} cites {ev!r}")
        if ev and evidence_times:
            if ev not in evidence_times:
                bad("evidence_unknown", f"step {i} cites unknown id {ev!r}")
            elif audit_at and evidence_times[ev] > audit_at:
                bad("evidence_after_audit_at", f"step {i} cites {ev} ({evidence_times[ev]}) > audit_at {audit_at}")

    well_formed = [s for s in steps if isinstance(s, dict) and s.get("operation") in OPERATION_PHASE
                   and s.get("phase") in PHASES and isinstance(s.get("system_id"), str)
                   and isinstance(s.get("resource_id"), str)]
    if len(well_formed) == len(steps):
        if status == "INCOMPLETE" and not any(s["phase"] == "FINDING" for s in steps):
            bad("incomplete_needs_finding", "INCOMPLETE without any FINDING step")
        keys = [step_sort_key(s) for s in steps]
        if keys != sorted(keys):
            first = next(i for i in range(1, len(keys)) if keys[i] < keys[i - 1])
            bad("step_order", f"step {first + 1} ({steps[first]['operation']} {steps[first]['resource_id']}) "
                              f"sorts before step {first} ({steps[first - 1]['operation']} {steps[first - 1]['resource_id']})")
        seen = set()
        for s in steps:
            k = (s["phase"], s["operation"], s["system_id"], s["resource_id"])
            if k in seen:
                bad("duplicate_step", f"{k} appears more than once")
            seen.add(k)
        # CONTRACT §11 invariant 6: no remediation without a finding on the same resource, and every remediated
        # resource has a verification step.
        by_phase = {ph: {(s["system_id"], s["resource_id"]) for s in steps if s["phase"] == ph} for ph in PHASES}
        for res in sorted(by_phase["REMEDIATION"] - by_phase["FINDING"]):
            bad("remediation_without_finding", f"{res[0]}/{res[1]} is remediated but has no FINDING")
        for res in sorted(by_phase["REMEDIATION"] - by_phase["VERIFICATION"]):
            bad("remediation_without_verification", f"{res[0]}/{res[1]} is remediated but never verified")


def validate(output_path: Path, requests_path: Path, dataset_dir: Path, max_examples: int) -> bool:
    problems: dict[str, list[str]] = defaultdict(list)

    with requests_path.open(newline="", encoding="utf-8") as f:
        requests = list(csv.DictReader(f))
    expected_ids = [r["case_id"] for r in requests]
    audit_at = {r["case_id"]: r["audit_at"] for r in requests}
    evidence_times = load_evidence_times(dataset_dir)

    if not output_path.exists():
        print(f"FAIL: {output_path} does not exist")
        return False
    with output_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = list(reader)

    if header != OUTPUT_HEADER:
        problems["header"].append(f"got {header!r}, expected {OUTPUT_HEADER!r}")
    if len(rows) != len(expected_ids):
        problems["row_count"].append(f"got {len(rows)} data rows, expected {len(expected_ids)} "
                                     f"(one per case in {requests_path.name})")

    got_ids = []
    for n, row in enumerate(rows, 2):
        if len(row) != 3:
            problems["column_count"].append(f"line {n}: {len(row)} columns")
            continue
        case_id, status, steps_json = row
        got_ids.append(case_id)
        if status not in STATUSES:
            problems["status_value"].append(f"{case_id}: deletion_status={status!r}")
        try:
            steps = json.loads(steps_json)
        except json.JSONDecodeError as e:
            problems["steps_json_invalid"].append(f"{case_id}: {e.msg} at char {e.pos}")
            continue
        if not isinstance(steps, list):
            problems["steps_json_not_array"].append(f"{case_id}: top-level {type(steps).__name__}")
            continue
        check_steps(case_id, status, steps, audit_at.get(case_id), evidence_times, problems)

    dupes = sorted({c for c in got_ids if got_ids.count(c) > 1}, key=case_sort_key)
    if dupes:
        problems["duplicate_case_id"].append(", ".join(dupes))
    missing = [c for c in expected_ids if c not in set(got_ids)]
    if missing:
        problems["missing_case_id"].append(f"{len(missing)} missing, e.g. {', '.join(missing[:5])}")
    unknown = [c for c in got_ids if c not in set(expected_ids)]
    if unknown:
        problems["unknown_case_id"].append(f"{len(unknown)} not in requests, e.g. {', '.join(unknown[:5])}")
    if expected_ids != sorted(expected_ids, key=case_sort_key):
        problems["requests_not_numeric_order"].append("input requests are not in numeric case_id order")
    if not missing and not unknown and not dupes and got_ids != expected_ids:
        first = next(i for i, (a, b) in enumerate(zip(got_ids, expected_ids)) if a != b)
        problems["row_order"].append(f"row {first + 1} is {got_ids[first]}, expected {expected_ids[first]} "
                                     f"(rows must follow numeric case_id order of the input file)")

    warnings = {k: v for k, v in problems.items() if k.startswith("WARN:")}
    problems = {k: v for k, v in problems.items() if not k.startswith("WARN:")}
    print(f"Validating {output_path.relative_to(REPO_ROOT) if output_path.is_relative_to(REPO_ROOT) else output_path} "
          f"against {requests_path.name}: {len(rows)} rows, {len(expected_ids)} expected")
    for check in sorted(warnings):
        print(f"  warning [{check[5:]}] {len(warnings[check])}")
        for item in warnings[check][:max_examples]:
            print(f"      - {item}")
    if not problems:
        print("PASS: all checks passed" + (f" ({sum(map(len, warnings.values()))} warning(s) above)" if warnings else ""))
        return True
    total = sum(len(v) for v in problems.values())
    print(f"FAIL: {total} problem(s) in {len(problems)} check(s)")
    for check in sorted(problems):
        items = problems[check]
        print(f"  [{check}] {len(items)}")
        for item in items[:max_examples]:
            print(f"      - {item}")
        if len(items) > max_examples:
            print(f"      ... {len(items) - max_examples} more")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, default=REPO_ROOT / "output.csv")
    ap.add_argument("--requests", type=Path, default=DATASET_DIR / "deletion_requests.csv")
    ap.add_argument("--dataset", type=Path, default=DATASET_DIR)
    ap.add_argument("--max-examples", type=int, default=5)
    args = ap.parse_args()
    return 0 if validate(args.output.resolve(), args.requests.resolve(), args.dataset.resolve(), args.max_examples) else 1


if __name__ == "__main__":
    sys.exit(main())
