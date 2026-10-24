"""Which rule explains the evidence cited for absence in the gold answers? (Round 1 citation question)

For every VERIFY_RECORD_ABSENT step with a non-empty evidence_id in the tuning split, list all evidence files
captured <= audit_at that show the resource absent (explicit NOT_FOUND / 404 / rows=0 observation, or omission
from a snapshot of the same system and category), then test three rules:
  R1 latest:         the latest such file <= audit_at
  R2 earliest_after: the earliest such file captured after the resource's deletion_worker DELETE event
  R3 same_file:      a file already cited by another step of the same case (if several, the latest)
  R4 explicit_first: the latest file with an explicit absence observation (NOT_FOUND / 404 / rows=0);
                     only if none exists, the latest snapshot that omits the resource (matches absence rule (c))
Usage: python3 code/evaluation/citation_rules.py

Decisions: evidence for D4 (citation rule R4, 8/8 on the tuning gold).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit import vocab
from audit.data import Dataset, read_csv
from audit.identity import IdentityResolver
from audit.parsers import build_index
from contract import DATASET_DIR
from evaluation.splits import tuning_ids


def main() -> int:
    ds = Dataset.load(DATASET_DIR)
    res = IdentityResolver(ds.identities, ds.audit_log)
    idx = build_index(ds.manifest, DATASET_DIR, res.unique_names)
    captured = {m["file_id"]: m["captured_at"] for m in ds.manifest}
    req = {r["case_id"]: r for r in read_csv(DATASET_DIR / "sample_deletion_requests.csv")}
    tune = set(tuning_ids())
    gold = {r["case_id"]: json.loads(r["steps_json"]) for r in read_csv(DATASET_DIR / "sample_output.csv") if r["case_id"] in tune}
    deletes = {}
    for a in ds.activity:
        if a["event_type"] == "DELETE_RECORD" and a["source_system"] == "deletion_worker":
            deletes.setdefault(a["resource_id"], []).append(a["event_time"])
    fits = {"R1_latest": 0, "R2_earliest_after_delete": 0, "R3_same_file_as_other_steps": 0, "R4_explicit_first": 0}
    n = 0
    print(f"{'case':<8} {'resource':<14} {'gold':<12} {'latest 3 candidates (file@captured)':<70} R1 R2 R3 R4")
    for cid in sorted(gold):
        T = req[cid]["audit_at"]
        for s in gold[cid]:
            if s["operation"] != "VERIFY_RECORD_ABSENT" or not s["evidence_id"]:
                continue
            rid, sysid = s["resource_id"], s["system_id"]
            cat = vocab.PREFIX_CATEGORY.get(vocab.resource_prefix(rid) or "")
            explicit = {o.file_id for o in idx.observations
                        if o.resource_id == rid and o.observed_state == vocab.ABSENT and o.captured_at <= T}
            cands = set(explicit)
            cands |= {c.file_id for c in idx.coverage
                      if c.system_id == sysid and c.category == cat and rid not in c.present and c.captured_at <= T}
            # a file that shows the resource PRESENT anywhere cannot prove absence
            cands -= {o.file_id for o in idx.observations if o.resource_id == rid and o.observed_state not in (None, vocab.ABSENT)}
            order = sorted(cands, key=lambda f: (captured[f], f))
            r1 = order[-1] if order else None
            dt = min(deletes.get(rid, ["9999"]))
            after = [f for f in order if captured[f] >= dt]
            r2 = after[0] if after else None
            others = {x["evidence_id"] for x in gold[cid] if x is not s and x["evidence_id"]}
            same = [f for f in order if f in others]
            r3 = same[-1] if same else None
            exp = [f for f in order if f in explicit]
            r4 = exp[-1] if exp else r1
            n += 1
            marks = []
            for name, pick in (("R1_latest", r1), ("R2_earliest_after_delete", r2), ("R3_same_file_as_other_steps", r3), ("R4_explicit_first", r4)):
                ok = pick == s["evidence_id"]
                fits[name] += ok
                marks.append("Y " if ok else "- ")
            cl = ", ".join(f"{f}{'*' if f in explicit else ''}@{captured[f][:10]}" for f in order[-3:])
            print(f"{cid:<8} {rid:<14} {s['evidence_id']:<12} {cl[:70]:<70} {' '.join(marks)}")
    print("(* = explicit absence observation; others prove absence by omission)")
    print(f"\nabsence citations in tuning gold: {n}")
    for k, v in fits.items():
        print(f"  {k:<30} fit {v}/{n} = {100 * v / n:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
