"""Participant-made evaluation cases E1-E5 (CONTRACT.md §13): inputs, expected output, pass/fail.

Each case is a synthetic request row over the real dataset (identities, evidence, activity). The expected output
is hand-derived from participant data only. The pipeline runs unchanged on these rows (code/main.py run()).

Usage: python3 code/evaluation/custom_cases.py
Intent: E1-E3 use new request texts. They were classified by the LLM once, in their own batch (main.py "extra texts"),
so the dataset's cached batches are untouched; that response is in code/llm_cache/. Runs use the frozen config, cache-only.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contract import DATASET_DIR, WORK_DIR

CACHE = WORK_DIR
FIELDS = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]


def eval_row(case_id: str) -> dict:
    with (DATASET_DIR / "deletion_requests.csv").open(newline="", encoding="utf-8") as f:
        return next(r for r in csv.DictReader(f) if r["case_id"] == case_id)


def cases() -> list[dict]:
    base153, base35 = eval_row("case_153"), eval_row("case_35")
    window = {"requested_at": "2026-09-14T04:20:00Z", "reported_status": "NOT_STARTED",
              "reported_at": "2026-09-15T22:20:00Z", "audit_at": "2026-09-17T09:20:00Z"}
    return [
        {"id": "E1", "proves": "deletion keyword + partial scope is not a deletion request",
         "row": {"case_id": "case_E1", "email": "kwame-mensah@proton.me", **window,
                 "request_text": "Please remove me from marketing lists only. Keep my account and orders."},
         "expected": "NOT_APPLICABLE, [] (although marketing_001 is linked to this subject in case_63)",
         "check": lambda out, dec, res: out["deletion_status"] == "NOT_APPLICABLE" and out["steps_json"] == "[]"},
        {"id": "E2", "proves": "negation in a non-English language",
         "row": {"case_id": "case_E2", "email": "amara-okeke@proton.me", **window,
                 "request_text": "No quiero eliminar mi cuenta; solo necesito una copia de mis datos."},
         "expected": "NOT_APPLICABLE, []",
         "check": lambda out, dec, res: out["deletion_status"] == "NOT_APPLICABLE" and out["steps_json"] == "[]"},
        {"id": "E3", "proves": "historical identity + non-deletion request (Japanese correction)",
         "row": {"case_id": "case_E3", "email": "minjun.kim90@gmail.com", **window,
                 "request_text": "電話番号を更新してください。アカウントは削除しないでください。"},
         "expected": "NOT_APPLICABLE, []; the email still resolves to user_12 through the audit log (historical)",
         "check": lambda out, dec, res: (out["deletion_status"] == "NOT_APPLICABLE" and out["steps_json"] == "[]"
                                         and res("minjun.kim90@gmail.com") == ("user_12", "historical"))},
        {"id": "E4", "proves": "inclusive cutoff: evidence captured 1 s after audit_at is never cited or used for state",
         "row": {**base153, "case_id": "case_E4", "audit_at": "2026-08-02T06:59:59Z"},
         "expected": "case_153 with audit_at 1 s before evidence_15 (captured 2026-08-02T07:00:00Z): no step cites evidence_15, "
                     "and account_8023 is not reported as remaining from it (the unmodified case_153 reports it from evidence_15)",
         "check": lambda out, dec, res: (
             all(s["evidence_id"] != "evidence_15" for s in json.loads(out["steps_json"]))
             and not any(s["resource_id"] == "account_8023" and s["phase"] == "FINDING" and s["evidence_id"] == "evidence_15"
                         for s in json.loads(out["steps_json"])))},
        {"id": "E5", "proves": "near-miss decoy rejection + screenshot extraction",
         "row": {**base35, "case_id": "case_E5"},
         "expected": "search_428 (user_13, screenshot evidence_09) is reported as remaining; search_1874 "
                     "(g.silva76.jr@gmail.com, a different person) appears in no step",
         "check": lambda out, dec, res: (
             any(s["resource_id"] == "search_428" and s["operation"] == "REPORT_REMAINING_RECORD" and s["evidence_id"] == "evidence_09"
                 for s in json.loads(out["steps_json"]))
             and all(s["resource_id"] != "search_1874" for s in json.loads(out["steps_json"])))},
    ]


def main() -> int:
    import main as pipeline
    from audit.data import Dataset
    from audit.identity import IdentityResolver

    cs = cases()
    req_path = CACHE / "custom" / "requests.csv"
    req_path.parent.mkdir(parents=True, exist_ok=True)
    with req_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(c["row"] for c in cs)
    out_path = CACHE / "custom" / "output.csv"
    run_dir = CACHE / "runs" / "custom"
    cfg, cfg_hash = pipeline.load_frozen(pipeline.FROZEN_DEFAULT)   # same frozen config as the production run, cache-only
    pl = cfg["pipeline"]
    pipeline.run(req_path, out_path, DATASET_DIR, pl["timing_links"], pl["post_audit_links"], run_dir, pl["absence_rule"],
                 pl["verify_unobserved"], pl["use_llm"], pl["post_audit_unit_links"], pl["unblocked_backup_rule"],
                 pl["strict_catalog_veto"], llm_config=pipeline.FROZEN_DEFAULT, cache_only=True,
                 header=(f"custom cases, config frozen_v1 sha256={cfg_hash}",))
    with out_path.open(newline="", encoding="utf-8") as f:
        out = {r["case_id"]: r for r in csv.DictReader(f)}
    dec = {json.loads(line)["case_id"]: json.loads(line) for line in (run_dir / "decisions.jsonl").open(encoding="utf-8")}
    ds = Dataset.load(DATASET_DIR)
    resolver = IdentityResolver(ds.identities, ds.audit_log)

    def res(email):
        s, how = resolver.resolve(email)
        return (s.user_id if s else None, how)

    passed = 0
    print("| Case | Proves | Expected | Actual status / steps | Intent source | Result |")
    print("|---|---|---|---|---|---|")
    for c in cs:
        cid = c["row"]["case_id"]
        o, d = out[cid], dec.get(cid, {})
        ok = bool(c["check"](o, d, res))
        passed += ok
        steps = json.loads(o["steps_json"])
        summary = ", ".join(f"{s['operation']} {s['resource_id']}" + (f" ({s['evidence_id']})" if s["evidence_id"] else "")
                            for s in steps if s["phase"] == "FINDING") or "no findings"
        print(f"| {c['id']} | {c['proves']} | {c['expected']} | {o['deletion_status']}, {len(steps)} steps; {summary} | "
              f"{d.get('intent', {}).get('source', '?')} | {'PASS' if ok else 'FAIL'} |")
    print(f"\n{passed}/{len(cs)} passed")
    return 0 if passed == len(cs) else 1


if __name__ == "__main__":
    sys.exit(main())
