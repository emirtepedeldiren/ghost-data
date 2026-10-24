"""Attribute every missing / extra step of a sample run to a root cause (tuning split only, D16).

Usage: python3 code/evaluation/rootcause.py --pred code/cache/X.csv --decisions code/cache/runs/X/decisions.jsonl [--cases]
Reads only participant data + sample_output.csv (gold) + the run's own decision log.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contract import DATASET_DIR  # noqa: E402
from evaluation.splits import tuning_ids  # noqa: E402


def where(rid: str, corpus: list[str], activity: str) -> str:
    pat = re.compile(r"(?<![\w])" + re.escape(rid) + r"(?!\d)")
    if any(pat.search(t) for t in corpus):
        return "evidence"
    return "activity_only" if pat.search(activity) else "nowhere"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", type=Path, required=True)
    ap.add_argument("--decisions", type=Path, required=True)
    ap.add_argument("--cases", action="store_true", help="also print per-case attribution (tuning cases only)")
    args = ap.parse_args()
    tune = set(tuning_ids())
    with (DATASET_DIR / "sample_output.csv").open(newline="", encoding="utf-8") as f:
        gold = {r["case_id"]: json.loads(r["steps_json"]) for r in csv.DictReader(f) if r["case_id"] in tune}
    with args.pred.open(newline="", encoding="utf-8") as f:
        pred = {r["case_id"]: json.loads(r["steps_json"]) for r in csv.DictReader(f)}
    dec = {}
    for line in args.decisions.open(encoding="utf-8"):
        d = json.loads(line)
        dec[d["case_id"]] = d
    corpus = [p.read_text(errors="replace") for p in (DATASET_DIR / "evidence").rglob("*")
              if p.is_file() and p.suffix not in (".png", ".pdf")]
    activity = (DATASET_DIR / "system_activity.csv").read_text()
    key = lambda s: (s["phase"], s["operation"], s["system_id"], s["resource_id"])  # noqa: E731
    miss, extra, per = collections.Counter(), collections.Counter(), collections.defaultdict(list)
    for cid in sorted(gold):
        g, p, d = gold[cid], pred.get(cid, []), dec.get(cid, {})
        res = {r["resource_id"]: r for r in d.get("resources", [])}
        pk = collections.Counter(map(key, p))
        for s in g:
            k = key(s)
            if pk[k] > 0:
                pk[k] -= 1
                continue
            rid = s["resource_id"]
            if not d.get("intent", {}).get("authorizes_deletion", True):
                c = "intent"
            elif rid not in res:
                w = where(rid, corpus, activity)
                obs_ids = [r for r in [rid] if w == "evidence"]
                c = {"activity_only": "linking: activity-only (timing)", "nowhere": "unreachable: id in no participant file",
                     "evidence": "linking: observed without identifiers"}[w] if not obs_ids else "linking: observed without identifiers"
            else:
                r = res[rid]
                if r["state_source"] == "coverage":
                    c = "state: snapshot omission overrode explicit observation"
                elif r["state"] is None:
                    c = "policy: linked, no state <= audit_at"
                else:
                    c = f"state/policy: state={r['state']} action={r['action']}"
            miss[c] += 1
            per[cid].append(f"MISSING {c}: {s['operation']} {rid}")
        gk = collections.Counter(map(key, g))
        for s in p:
            k = key(s)
            if gk[k] > 0:
                gk[k] -= 1
                continue
            links = " ".join(res.get(s["resource_id"], {}).get("links", []))
            c = ("state/policy: same resource, different operation" if s["resource_id"] in {x["resource_id"] for x in g}
                 else "linking: over-link via catalog" if "catalog" in links
                 else "linking: over-link via timing" if "timing" in links else "linking: over-link via identifier evidence")
            extra[c] += 1
            per[cid].append(f"EXTRA   {c}: {s['operation']} {s['resource_id']}")
    print(f"tuning cases: {len(gold)}  missing gold steps: {sum(miss.values())}  extra steps: {sum(extra.values())}")
    for k, v in miss.most_common():
        print(f"  missing {v:>3}  {k}")
    for k, v in extra.most_common():
        print(f"  extra   {v:>3}  {k}")
    if args.cases:
        for cid in sorted(per):
            for line in per[cid]:
                print(f"    {cid}: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
