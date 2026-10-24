"""Ghost Data Still Exists - deletion-audit agent (entry point).

Usage:
    python3 code/main.py                                   # 518 eval requests -> ./output.csv
    python3 code/main.py --requests dataset/sample_deletion_requests.csv --output code/cache/sample_predictions.csv
Options (defaults are the approved decisions; each flag switches one off for measurement):
    --no-timing-links       disable D20 (L2 worker-schedule linker, global unique assignment)
    --no-post-audit-links   disable D12 (identity links from files captured after audit_at)
    --no-post-audit-unit-links  disable D21 (D12 links from units whose own timestamp is after audit_at)
    --no-unblocked-backup-rule  disable D22 (backup holding subject data, no block observed => restore risk)
    --strict-catalog-veto   original D13 instead of revised D13 (any foreign identity vetoes a catalog link)
    --run-name NAME         run log and decision log go to code/cache/runs/NAME/

Pipeline (CONTRACT.md §4): intent -> identity -> evidence index -> linking -> state at audit_at ->
policy -> steps (code/contract.py ordering) -> CSV.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

from audit import intent as intent_mod  # noqa: E402
from audit.data import Dataset, read_csv  # noqa: E402
from audit.identity import IdentityResolver  # noqa: E402
from audit.linking import OwnershipIndex, TimingLinker, catalog_links, explicit_links  # noqa: E402
from audit.parsers import build_index  # noqa: E402
from audit.reconstruct import StateIndex, build_steps  # noqa: E402
from llm import LLMClient  # noqa: E402
from contract import DATASET_DIR, OUTPUT_HEADER, REPO_ROOT, case_sort_key, dumps_steps  # noqa: E402


def run(requests_path: Path, output_path: Path, dataset_dir: Path, timing_links: bool,
        post_audit_links: bool, run_dir: Path, absence_rule: str = "fallback_delete", verify_unobserved: bool = True,
        use_llm: bool = True, post_audit_unit_links: bool = True, unblocked_backup_rule: bool = True,
        strict_catalog_veto: bool = False) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        log_lines.append(msg)

    ds = Dataset.load(dataset_dir)
    requests = read_csv(requests_path)
    resolver = IdentityResolver(ds.identities, ds.audit_log)
    llm_client = LLMClient() if use_llm else None
    index = build_index(ds.manifest, dataset_dir, resolver.unique_names, llm_client)
    states = StateIndex(index, ds.activity, ds.rules)
    ownership = OwnershipIndex(index.observations, resolver)
    by_ident = defaultdict(list)
    for o in index.observations:
        for i in o.identifiers_present:
            by_ident[i].append(o)
    log(f"run started {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    log(f"requests={requests_path.name} n={len(requests)} timing_links={timing_links} post_audit_links={post_audit_links} "
        f"post_audit_unit_links={post_audit_unit_links} unblocked_backup_rule={unblocked_backup_rule} "
        f"strict_catalog_veto={strict_catalog_veto} absence_rule={absence_rule} verify_unobserved={verify_unobserved}")
    log(f"evidence: {len(ds.manifest)} files, {len(index.observations)} observations, {len(index.coverage)} coverage snapshots")
    classifier = intent_mod.IntentClassifier(llm_client)
    for line in classifier.prepare(r["request_text"] for r in ds.all_requests()):
        log(line)
    # Texts outside the dataset (e.g. custom evaluation cases) get their own batches, so the dataset's cached
    # batches keep their boundaries and stay valid.
    extra = [r["request_text"] for r in requests if r["request_text"] not in classifier.results]
    if extra and use_llm:
        for line in classifier.prepare(extra):
            log(f"extra texts: {line}")
    for line in classifier.disagreements(ds.all_requests()):
        log(line)
    log(f"intent: {'LLM ' + classifier.client.models['intent'] + ' (cached)' if use_llm else intent_mod.TEMPORARY_FALLBACK_LABEL}")
    timing = (TimingLinker(ds.all_requests(), ds.activity, ownership, resolver,
                           lambda text: classifier.classify(text).authorizes_deletion) if timing_links else None)
    for p in index.pending:
        log(f"PENDING {p}")
    for w in index.warnings:
        log(f"WARNING {w}")

    rows, decisions = [], []
    stats = Counter()
    for req in requests:
        cid, audit_at = req["case_id"], req["audit_at"]
        it = classifier.classify(req["request_text"])
        if not it.source.startswith("llm"):
            log(f"{cid}: intent via {it.source}")
        decision = {"case_id": cid, "audit_at": audit_at, "intent": it.__dict__}
        if not it.authorizes_deletion:
            rows.append((cid, "NOT_APPLICABLE", "[]"))
            stats["NOT_APPLICABLE"] += 1
            decisions.append(decision)
            continue
        subject, how = resolver.resolve(req["email"])
        if subject is None:
            log(f"{cid}: identity not resolved ({how})")
            decision["identity"] = how
            rows.append((cid, "COMPLETE", "[]"))
            stats["unresolved_identity"] += 1
            decisions.append(decision)
            continue
        decision["subject"] = {"user_id": subject.user_id, "resolution": how, "identifiers": len(subject.identifiers)}

        candidate_obs = {id(o): o for i in subject.identifiers for o in by_ident.get(i, [])}.values()
        links = explicit_links(subject, audit_at, list(candidate_obs), resolver, post_audit_links, post_audit_unit_links)
        cat_links, rejected, flags = catalog_links(subject, audit_at, ds.activity, ownership, states.obs_by_rid,
                                                   strict_catalog_veto)
        links += cat_links
        if timing:
            links += timing.assigned.get(cid, [])
            for amb in timing.ambiguous.get(cid, []):
                log(f"{cid}: timing ambiguous, left unlinked: {amb}")
        for r in rejected:
            log(f"{cid}: catalog link rejected: {r}")
        for fl in flags:
            log(f"{cid}: catalog link kept, FLAG {fl}")
        via_by_rid = defaultdict(set)
        for l in links:
            via_by_rid[l.resource_id].add(f"{l.via}:{l.source_id}")
            if l.via.startswith("identifier_post_audit"):
                log(f"{cid}: post-audit link {l.resource_id} via {l.source_id} ({l.locator})")

        resolved = []
        for rid in sorted(via_by_rid):
            rs = states.resolve(rid, audit_at, absence_rule, subject.identifiers, unblocked_backup_rule)
            vias = via_by_rid[rid]
            # D21: a resource linked only through post-audit units needs its own state observation <= audit_at.
            if all(v.startswith("identifier_post_audit_unit") for v in vias) and rs.source != "observation":
                log(f"{cid}: D21 link {rid} dropped, no state observation <= audit_at ({sorted(vias)[0]})")
                continue
            rs.linked_by_evidence = any(v.startswith("identifier") and not v.startswith("identifier_post_audit_unit")
                                        for v in vias)
            if rs.condition and " met:" not in rs.condition:
                log(f"{cid}: condition {rid}: {rs.condition}")   # D23: undeterminable / not met are explicit
                stats["condition_" + rs.condition.split(":")[0].split(" ", 1)[1].replace(" ", "_")] += 1
            resolved.append(rs)
        status, steps, notes = build_steps(resolved, verify_unobserved)
        if not steps:
            log(f"{cid}: no evidence-backed step for {len(resolved)} linked resource(s)")
            stats["no_steps"] += 1
        stats[status] += 1
        decision["resources"] = [{
            "resource_id": rs.resource_id, "system_id": rs.system_id, "links": sorted(via_by_rid[rs.resource_id]),
            "state": rs.state, "evidence_id": rs.evidence_id, "state_source": rs.source,
            "category": rs.category, "category_source": rs.category_source, "action": rs.action, "notes": rs.notes,
            "condition": rs.condition,
        } for rs in resolved]
        decision["skipped"] = notes
        decisions.append(decision)
        rows.append((cid, status, dumps_steps(steps)))

    rows.sort(key=lambda r: case_sort_key(r[0]))
    input_order = [r["case_id"] for r in requests]
    if [r[0] for r in rows] != input_order:
        log("WARNING output order differs from input order (input not in numeric case order)")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(OUTPUT_HEADER)
        w.writerows(rows)

    log(f"done: {dict(stats)} -> {output_path}")
    (run_dir / "run_log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    with (run_dir / "decisions.jsonl").open("w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--requests", type=Path, default=DATASET_DIR / "deletion_requests.csv")
    ap.add_argument("--output", type=Path, default=REPO_ROOT / "output.csv")
    ap.add_argument("--dataset", type=Path, default=DATASET_DIR)
    ap.add_argument("--no-timing-links", action="store_true")
    ap.add_argument("--no-post-audit-links", action="store_true")
    ap.add_argument("--no-post-audit-unit-links", action="store_true")
    ap.add_argument("--no-unblocked-backup-rule", action="store_true")
    ap.add_argument("--strict-catalog-veto", action="store_true")
    ap.add_argument("--run-name", default="latest")
    ap.add_argument("--absence-rule", choices=("scope", "fallback", "fallback_delete", "override"), default="fallback_delete")
    ap.add_argument("--no-verify-unobserved", action="store_true", help="disable fix 2")
    ap.add_argument("--no-llm", action="store_true", help="keyword intent fallback instead of the cached LLM")
    args = ap.parse_args()
    stats = run(args.requests.resolve(), args.output.resolve(), args.dataset.resolve(), not args.no_timing_links,
                not args.no_post_audit_links, CODE_DIR / "cache" / "runs" / args.run_name,
                args.absence_rule, not args.no_verify_unobserved, not args.no_llm,
                not args.no_post_audit_unit_links, not args.no_unblocked_backup_rule, args.strict_catalog_veto)
    print(f"{args.output}: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
