"""Score a predictions CSV against a gold CSV (default: dataset/sample_output.csv).

Usage:
    python3 code/evaluation/main.py --pred predictions.csv [--gold dataset/sample_output.csv]
                                    [--json-out report.json] [--max-diff-cases 50]

Metrics (CONTRACT.md §12). Only cases present in the gold file are scored. A gold case with
no prediction is scored as status "<missing>" with no steps.

  status accuracy   share of gold cases whose deletion_status matches.
  step-level        a step is the tuple (phase, system_id, resource_id, operation, state,
                    evidence_id); step_no is ignored. Per case, matches are counted as a
                    multiset intersection. Reported as precision, recall, F1 and
                    accuracy = matches / sum(max(|gold|, |pred|)). The same is also reported
                    for the identity key (phase, operation, system_id, resource_id), which
                    separates discovery errors from state/citation errors.
  field-level       gold and predicted steps are aligned with difflib on the identity key
                    (equal runs pair 1:1, replaced runs pair by position, leftovers stay
                    unpaired). Field accuracy = paired steps where the field matches /
                    (pairs + unpaired gold + unpaired pred), for system_id, resource_id,
                    operation, state, evidence_id (plus phase).
  sequence          share of gold cases whose full steps_json array (all fields including
                    step_no, in order) equals the gold array exactly.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contract import DATASET_DIR, STEP_FIELDS, STEP_KEYS, case_sort_key  # noqa: E402

FIELD_METRICS = ("system_id", "resource_id", "operation", "state", "evidence_id", "phase")
IDENTITY = ("phase", "operation", "system_id", "resource_id")


def load_cases(path: Path) -> tuple[dict[str, dict], list[str]]:
    """case_id -> {status, steps, error}. Malformed rows never raise; they are recorded."""
    cases: dict[str, dict] = {}
    warnings: list[str] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing_cols = {"case_id", "deletion_status", "steps_json"} - set(reader.fieldnames or [])
        if missing_cols:
            warnings.append(f"{path.name}: missing columns {sorted(missing_cols)}")
        for row in reader:
            case_id = (row.get("case_id") or "").strip()
            if not case_id:
                continue
            error = None
            try:
                steps = json.loads(row.get("steps_json") or "")
                if not isinstance(steps, list):
                    error, steps = f"steps_json is {type(steps).__name__}, not an array", []
            except json.JSONDecodeError as e:
                error, steps = f"invalid steps_json ({e.msg})", []
            steps = [s for s in steps if isinstance(s, dict)]
            if case_id in cases:
                warnings.append(f"{path.name}: duplicate {case_id}, keeping the first row")
                continue
            cases[case_id] = {"status": (row.get("deletion_status") or "").strip(), "steps": steps, "error": error}
    return cases, warnings


def full(step: dict) -> tuple:
    return tuple(step.get(k) for k in STEP_FIELDS)


def ident(step: dict) -> tuple:
    return tuple(step.get(k) for k in IDENTITY)


def align(gold: list[dict], pred: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    sm = difflib.SequenceMatcher(None, [ident(s) for s in gold], [ident(s) for s in pred], autojunk=False)
    pairs, g_only, p_only = [], [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        g, p = gold[i1:i2], pred[j1:j2]
        if tag in ("equal", "replace"):
            n = min(len(g), len(p))
            pairs.extend(zip(g[:n], p[:n]))
            g_only.extend(g[n:])
            p_only.extend(p[n:])
        elif tag == "delete":
            g_only.extend(g)
        elif tag == "insert":
            p_only.extend(p)
    return pairs, g_only, p_only


def prf(tp: int, n_pred: int, n_gold: int, denom_max: int) -> dict:
    p = tp / n_pred if n_pred else (1.0 if n_gold == 0 else 0.0)
    r = tp / n_gold if n_gold else (1.0 if n_pred == 0 else 0.0)
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    acc = tp / denom_max if denom_max else 1.0
    return {"precision": p, "recall": r, "f1": f1, "accuracy": acc, "matched": tp, "pred_steps": n_pred, "gold_steps": n_gold}


def fmt_step(s: dict) -> str:
    ev = s.get("evidence_id") or '""'
    return f"{s.get('phase', '?')[:5]} {s.get('operation')} {s.get('system_id')}/{s.get('resource_id')} {s.get('state')} ev={ev}"


def case_diff(gold: dict, pred: dict) -> dict:
    g, p = gold["steps"], pred["steps"]
    gi, pi = Counter(map(ident, g)), Counter(map(ident, p))
    missing = [s for s in g if (gi - pi)[ident(s)] > 0]
    extra = [s for s in p if (pi - gi)[ident(s)] > 0]
    # Same identity key on both sides but other fields differ.
    p_by_id: dict[tuple, list[dict]] = {}
    for s in p:
        p_by_id.setdefault(ident(s), []).append(s)
    wrong = []
    for s in g:
        cands = p_by_id.get(ident(s))
        if cands:
            q = cands.pop(0)
            diffs = {k: (s.get(k), q.get(k)) for k in STEP_KEYS if s.get(k) != q.get(k) and k != "step_no"}
            if diffs:
                wrong.append({"step": fmt_step(s), "fields": diffs})
    order_only = not missing and not extra and not wrong and [ident(s) for s in g] != [ident(s) for s in p]
    step_no_bad = [i for i, s in enumerate(p, 1) if s.get("step_no") != i]
    return {
        "status": (gold["status"], pred["status"]),
        "missing": [fmt_step(s) for s in missing],
        "extra": [fmt_step(s) for s in extra],
        "wrong_fields": wrong,
        "order_differs": order_only,
        "step_no_not_consecutive": step_no_bad[:3],
        "parse_error": pred.get("error"),
    }


def score(gold_cases: dict[str, dict], pred_cases: dict[str, dict]) -> dict:
    ids = sorted(gold_cases, key=case_sort_key)
    status_ok = seq_ok = 0
    by_status: dict[str, Counter] = {}
    confusion: Counter = Counter()
    tp_full = tp_id = n_pred = n_gold = denom = 0
    field_ok = Counter()
    field_denom = 0
    diffs = {}
    missing_preds = []

    for cid in ids:
        g = gold_cases[cid]
        p = pred_cases.get(cid)
        if p is None:
            missing_preds.append(cid)
            p = {"status": "<missing>", "steps": [], "error": "no prediction row"}
        gs, ps = g["steps"], p["steps"]

        s_ok = g["status"] == p["status"]
        status_ok += s_ok
        confusion[(g["status"], p["status"])] += 1
        bucket = by_status.setdefault(g["status"], Counter())
        bucket["cases"] += 1
        bucket["status_ok"] += s_ok

        tp_full += sum((Counter(map(full, gs)) & Counter(map(full, ps))).values())
        tp_id += sum((Counter(map(ident, gs)) & Counter(map(ident, ps))).values())
        n_gold += len(gs)
        n_pred += len(ps)
        denom += max(len(gs), len(ps))

        pairs, g_only, p_only = align(gs, ps)
        field_denom += len(pairs) + len(g_only) + len(p_only)
        for a, b in pairs:
            for k in FIELD_METRICS:
                field_ok[k] += a.get(k) == b.get(k)

        exact = [{k: s.get(k) for k in STEP_KEYS} for s in gs] == [{k: s.get(k) for k in STEP_KEYS} for s in ps]
        seq_ok += exact
        bucket["sequence_ok"] += exact
        if not (exact and s_ok):
            diffs[cid] = case_diff(g, p)

    n = len(ids)
    return {
        "cases": n,
        "missing_predictions": missing_preds,
        "extra_predictions_ignored": sorted(set(pred_cases) - set(gold_cases), key=case_sort_key),
        "status_accuracy": status_ok / n if n else 0.0,
        "status_by_gold": {k: {"cases": v["cases"], "status_accuracy": v["status_ok"] / v["cases"],
                               "sequence_accuracy": v["sequence_ok"] / v["cases"]} for k, v in sorted(by_status.items())},
        "status_confusion": {f"{g}->{p}": c for (g, p), c in sorted(confusion.items())},
        "step_level": prf(tp_full, n_pred, n_gold, denom),
        "step_identity_level": prf(tp_id, n_pred, n_gold, denom),
        "field_level": {k: (field_ok[k] / field_denom if field_denom else 1.0) for k in FIELD_METRICS},
        "field_level_denominator": field_denom,
        "sequence_accuracy": seq_ok / n if n else 0.0,
        "case_diffs": diffs,
    }


def print_report(r: dict, max_diff_cases: int) -> None:
    pct = lambda x: f"{100 * x:6.2f}%"  # noqa: E731
    print(f"Cases scored: {r['cases']}   missing predictions: {len(r['missing_predictions'])}   "
          f"extra predictions ignored: {len(r['extra_predictions_ignored'])}")
    print(f"\nCase-level status accuracy : {pct(r['status_accuracy'])}")
    for k, v in r["status_by_gold"].items():
        print(f"    gold {k:<15} n={v['cases']:<3} status {pct(v['status_accuracy'])}   sequence {pct(v['sequence_accuracy'])}")
    print("    confusion (gold->pred): " + ", ".join(f"{k}={v}" for k, v in r["status_confusion"].items()))
    for name, key in (("Step-level (full step)", "step_level"), ("Step-level (identity key)", "step_identity_level")):
        s = r[key]
        print(f"\n{name:<26}: accuracy {pct(s['accuracy'])}  P {pct(s['precision'])}  R {pct(s['recall'])}  "
              f"F1 {pct(s['f1'])}  (matched {s['matched']}, gold {s['gold_steps']}, pred {s['pred_steps']})")
    print(f"\nField-level accuracy (aligned, denominator {r['field_level_denominator']}):")
    for k, v in r["field_level"].items():
        print(f"    {k:<12} {pct(v)}")
    print(f"\nSequence accuracy          : {pct(r['sequence_accuracy'])}")

    diffs = r["case_diffs"]
    print(f"\nPer-case differences: {len(diffs)} case(s)" + ("" if len(diffs) <= max_diff_cases else f" (showing {max_diff_cases})"))
    for cid in list(diffs)[:max_diff_cases]:
        d = diffs[cid]
        g_st, p_st = d["status"]
        head = f"  {cid}: status {g_st}" + ("" if g_st == p_st else f" -> predicted {p_st}")
        if d["parse_error"]:
            head += f"  [{d['parse_error']}]"
        print(head)
        for s in d["missing"]:
            print(f"      MISSING  {s}")
        for s in d["extra"]:
            print(f"      EXTRA    {s}")
        for w in d["wrong_fields"]:
            fields = ", ".join(f"{k}: gold={a!r} pred={b!r}" for k, (a, b) in w["fields"].items())
            print(f"      WRONG    {w['step']}  ->  {fields}")
        if d["order_differs"]:
            print("      ORDER    same steps, different order")
        if d["step_no_not_consecutive"]:
            print(f"      STEP_NO  not consecutive at positions {d['step_no_not_consecutive']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pred", type=Path, required=True, help="predictions CSV (case_id,deletion_status,steps_json)")
    ap.add_argument("--gold", type=Path, default=DATASET_DIR / "sample_output.csv")
    ap.add_argument("--json-out", type=Path, help="write the full metrics and diffs as JSON")
    ap.add_argument("--max-diff-cases", type=int, default=50)
    args = ap.parse_args()

    for path in (args.pred, args.gold):
        if not path.exists():
            print(f"ERROR: {path} does not exist", file=sys.stderr)
            return 2
    gold, gw = load_cases(args.gold)
    pred, pw = load_cases(args.pred)
    for w in gw + pw:
        print(f"WARNING: {w}")
    report = score(gold, pred)
    print_report(report, args.max_diff_cases)
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=list), encoding="utf-8")
        print(f"\nJSON report written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
