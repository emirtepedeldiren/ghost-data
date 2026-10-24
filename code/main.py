"""Ghost Data Still Exists - deletion-audit agent (entry point).

Usage:
    python3 code/main.py                                   # frozen run (code/config/frozen_v1.toml): 518 eval requests -> ./output.csv
    python3 code/main.py --requests dataset/sample_deletion_requests.csv --output .work/sample_predictions.csv
The frozen config pins models, prompt hashes, thresholds and flags, and runs cache-only (no API calls, no key needed).
Options (each flag switches one approved decision off, for measurement; the run log records the deviation):
    --no-timing-links       disable D20 (L2 worker-schedule linker, global unique assignment)
    --no-post-audit-links   disable D12 (identity links from files captured after audit_at)
    --no-post-audit-unit-links  disable D21 (D12 links from units whose own timestamp is after audit_at)
    --no-unblocked-backup-rule  disable D22 (backup holding subject data, no block observed => restore risk)
    --strict-catalog-veto   original D13 instead of revised D13 (any foreign identity vetoes a catalog link)
    --run-name NAME         run log and decision log go to .work/runs/NAME/
    --config PATH           frozen config (default code/config/frozen_v1.toml)
    --live                  allow live API calls on cache misses (development only; never used for the frozen output)

Pipeline (CONTRACT.md §4): intent -> identity -> evidence index -> linking -> state at audit_at ->
policy -> steps (code/contract.py ordering) -> CSV.

Decisions: wires D1–D27; the frozen config pins D20–D23 and D26; D27 logs NO_OBSERVABLE_RECORDS.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

from audit import intent as intent_mod  # noqa: E402
from audit.data import Dataset, read_csv  # noqa: E402
from audit.identity import IdentityResolver, Subject  # noqa: E402
from audit.linking import OwnershipIndex, TimingLinker, catalog_links, explicit_links  # noqa: E402
from audit.parsers import build_index  # noqa: E402
from audit.reconstruct import ResourceState, StateIndex, build_steps  # noqa: E402
from llm import CONFIG_PATH, LLMClient  # noqa: E402
from contract import DATASET_DIR, OUTPUT_HEADER, REPO_ROOT, WORK_DIR, case_sort_key, dumps_steps  # noqa: E402

FROZEN_DEFAULT = CODE_DIR / "config" / "frozen_v1.toml"


@dataclass(frozen=True)
class Options:
    """Pipeline switches of one run (the frozen config sets them; CLI flags can switch one off for measurement)."""
    timing_links: bool
    post_audit_links: bool
    absence_rule: str
    verify_unobserved: bool
    use_llm: bool
    post_audit_unit_links: bool
    unblocked_backup_rule: bool
    strict_catalog_veto: bool


@dataclass
class RunContext:
    """Everything built once per run and shared by every request."""
    ds: Dataset
    requests: list[dict]
    resolver: IdentityResolver
    states: StateIndex
    ownership: OwnershipIndex
    by_ident: dict
    classifier: intent_mod.IntentClassifier
    timing: TimingLinker | None
    stats: Counter = field(default_factory=Counter)


def run(requests_path: Path, output_path: Path, dataset_dir: Path, timing_links: bool,
        post_audit_links: bool, run_dir: Path, absence_rule: str = "fallback_delete", verify_unobserved: bool = True,
        use_llm: bool = True, post_audit_unit_links: bool = True, unblocked_backup_rule: bool = True,
        strict_catalog_veto: bool = False, llm_config: Path = CONFIG_PATH, cache_only: bool | None = None,
        header: tuple[str, ...] = ()) -> dict:
    """Audit every request in `requests_path` and write the output CSV, a run log and a per-case decision log.

    Steps:
      1. prepare_run: load the dataset, resolve identities, build the evidence index (screenshots via the cached LLM),
         classify intent in deterministic batches (D1, D18), build the L2 timing linker (D20).
      2. audit_request, per request: NOT_APPLICABLE if the text does not authorize deletion; otherwise resolve the
         subject, collect_links (identifier, post-audit D12/D21, catalog D13, timing D20), resolve_states at audit_at
         (D19, D22, D23) and build the ordered steps (D6). An empty result is logged as NO_OBSERVABLE_RECORDS (D27).
      3. write_outputs: sort rows by case_id (D7), write the CSV, the run log and decisions.jsonl.
    Returns the run statistics.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = list(header)

    def log(msg: str) -> None:
        log_lines.append(msg)

    opts = Options(timing_links, post_audit_links, absence_rule, verify_unobserved, use_llm, post_audit_unit_links,
                   unblocked_backup_rule, strict_catalog_veto)
    ctx = prepare_run(requests_path, dataset_dir, opts, llm_config, cache_only, log)
    rows, decisions = [], []
    for req in ctx.requests:
        row, decision = audit_request(req, ctx, opts, log)
        rows.append(row)
        decisions.append(decision)
    write_outputs(rows, decisions, ctx, output_path, run_dir, log_lines, log)
    return dict(ctx.stats)


def prepare_run(requests_path: Path, dataset_dir: Path, opts: Options, llm_config: Path, cache_only: bool | None,
                log: Callable[[str], None]) -> RunContext:
    """Step 1: load inputs, build the evidence index, classify intent, build the timing linker."""
    ds = Dataset.load(dataset_dir)
    requests = read_csv(requests_path)
    resolver = IdentityResolver(ds.identities, ds.audit_log)
    llm_client = LLMClient(llm_config, cache_only) if opts.use_llm else None
    index = build_index(ds.manifest, dataset_dir, resolver.unique_names, llm_client)
    states = StateIndex(index, ds.activity, ds.rules)
    ownership = OwnershipIndex(index.observations, resolver)
    by_ident = defaultdict(list)
    for o in index.observations:
        for i in o.identifiers_present:
            by_ident[i].append(o)
    log(f"run started {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    log(f"requests={requests_path.name} n={len(requests)} timing_links={opts.timing_links} post_audit_links={opts.post_audit_links} "
        f"post_audit_unit_links={opts.post_audit_unit_links} unblocked_backup_rule={opts.unblocked_backup_rule} "
        f"strict_catalog_veto={opts.strict_catalog_veto} absence_rule={opts.absence_rule} verify_unobserved={opts.verify_unobserved}")
    log(f"evidence: {len(ds.manifest)} files, {len(index.observations)} observations, {len(index.coverage)} coverage snapshots")
    classifier = intent_mod.IntentClassifier(llm_client)
    for line in classifier.prepare(r["request_text"] for r in ds.all_requests()):
        log(line)
    # Texts outside the dataset (e.g. custom evaluation cases) get their own batches, so the dataset's cached
    # batches keep their boundaries and stay valid.
    extra = [r["request_text"] for r in requests if r["request_text"] not in classifier.results]
    if extra and opts.use_llm:
        for line in classifier.prepare(extra):
            log(f"extra texts: {line}")
    for line in classifier.disagreements(ds.all_requests()):
        log(line)
    log(f"intent: {'LLM ' + classifier.client.models['intent'] + ' (cached)' if opts.use_llm else intent_mod.KEYWORD_FALLBACK_LABEL}")
    timing = (TimingLinker(ds.all_requests(), ds.activity, ownership, resolver,
                           lambda text: classifier.classify(text).authorizes_deletion) if opts.timing_links else None)
    for p in index.pending:
        log(f"PENDING {p}")
    for w in index.warnings:
        log(f"WARNING {w}")
    return RunContext(ds, requests, resolver, states, ownership, by_ident, classifier, timing)


def audit_request(req: dict, ctx: RunContext, opts: Options, log: Callable[[str], None]) -> tuple[tuple[str, str, str], dict]:
    """Step 2, one request: -> (output row, decision-log entry)."""
    stats = ctx.stats
    cid, audit_at = req["case_id"], req["audit_at"]
    it = ctx.classifier.classify(req["request_text"])
    if not it.source.startswith("llm"):
        log(f"{cid}: intent via {it.source}")
    decision = {"case_id": cid, "audit_at": audit_at, "intent": it.__dict__}
    if not it.authorizes_deletion:
        stats["NOT_APPLICABLE"] += 1
        return (cid, "NOT_APPLICABLE", "[]"), decision
    subject, how = ctx.resolver.resolve(req["email"])
    if subject is None:
        log(f"{cid}: NO_OBSERVABLE_RECORDS (D27): identity not resolved ({how})")
        decision["identity"] = how
        decision["no_observable_records"] = True
        stats["NO_OBSERVABLE_RECORDS"] += 1
        stats["unresolved_identity"] += 1
        return (cid, "COMPLETE", "[]"), decision
    decision["subject"] = {"user_id": subject.user_id, "resolution": how, "identifiers": len(subject.identifiers)}

    via_by_rid = collect_links(cid, audit_at, subject, ctx, opts, log)
    resolved = resolve_states(cid, audit_at, subject, via_by_rid, ctx, opts, log)
    status, steps, notes = build_steps(resolved, opts.verify_unobserved)
    if not steps:
        # D27: explicit branch. The schema has no status that is both honest and provable for a subject with no
        # observable record <= audit_at; the row stays COMPLETE with [], logged and flagged by the validator.
        log(f"{cid}: NO_OBSERVABLE_RECORDS (D27): no evidence-backed step for {len(resolved)} linked resource(s)")
        decision["no_observable_records"] = True
        stats["NO_OBSERVABLE_RECORDS"] += 1
    stats[status] += 1
    decision["resources"] = [{
        "resource_id": rs.resource_id, "system_id": rs.system_id, "links": sorted(via_by_rid[rs.resource_id]),
        "state": rs.state, "evidence_id": rs.evidence_id, "state_source": rs.source,
        "category": rs.category, "category_source": rs.category_source, "action": rs.action, "notes": rs.notes,
        "condition": rs.condition,
    } for rs in resolved]
    decision["skipped"] = notes
    return (cid, status, dumps_steps(steps)), decision


def collect_links(cid: str, audit_at: str, subject: Subject, ctx: RunContext, opts: Options,
                  log: Callable[[str], None]) -> dict[str, set[str]]:
    """Identifier (incl. post-audit, D12/D21), catalog (D13) and timing (D20) links -> resource_id -> {"via:source"}."""
    candidate_obs = {id(o): o for i in subject.identifiers for o in ctx.by_ident.get(i, [])}.values()
    links = explicit_links(subject, audit_at, list(candidate_obs), ctx.resolver, opts.post_audit_links,
                           opts.post_audit_unit_links)
    cat_links, rejected, flags = catalog_links(subject, audit_at, ctx.ds.activity, ctx.ownership, ctx.states.obs_by_rid,
                                               opts.strict_catalog_veto)
    links += cat_links
    if ctx.timing:
        links += ctx.timing.assigned.get(cid, [])
        for amb in ctx.timing.ambiguous.get(cid, []):
            log(f"{cid}: timing ambiguous, left unlinked: {amb}")
    for r in rejected:
        log(f"{cid}: catalog link rejected: {r}")
    for fl in flags:
        log(f"{cid}: catalog link kept, FLAG {fl}")
    via_by_rid = defaultdict(set)
    for link in links:
        via_by_rid[link.resource_id].add(f"{link.via}:{link.source_id}")
        if link.via.startswith("identifier_post_audit"):
            log(f"{cid}: post-audit link {link.resource_id} via {link.source_id} ({link.locator})")
    return via_by_rid


def resolve_states(cid: str, audit_at: str, subject: Subject, via_by_rid: dict[str, set[str]], ctx: RunContext,
                   opts: Options, log: Callable[[str], None]) -> list[ResourceState]:
    """State at audit_at for every linked resource (D19, D22, D23); D21 links need their own state observation."""
    resolved = []
    for rid in sorted(via_by_rid):
        rs = ctx.states.resolve(rid, audit_at, opts.absence_rule, subject.identifiers, opts.unblocked_backup_rule)
        vias = via_by_rid[rid]
        # D21: a resource linked only through post-audit units needs its own state observation <= audit_at.
        if all(v.startswith("identifier_post_audit_unit") for v in vias) and rs.source != "observation":
            log(f"{cid}: D21 link {rid} dropped, no state observation <= audit_at ({sorted(vias)[0]})")
            continue
        rs.linked_by_evidence = any(v.startswith("identifier") and not v.startswith("identifier_post_audit_unit")
                                    for v in vias)
        if rs.condition and " met:" not in rs.condition:
            log(f"{cid}: condition {rid}: {rs.condition}")   # D23: undeterminable / not met are explicit
            ctx.stats["condition_" + rs.condition.split(":")[0].split(" ", 1)[1].replace(" ", "_")] += 1
        resolved.append(rs)
    return resolved


def write_outputs(rows: list[tuple[str, str, str]], decisions: list[dict], ctx: RunContext, output_path: Path, run_dir: Path,
                  log_lines: list[str], log: Callable[[str], None]) -> None:
    """Step 3: rows in case_id order (D7) -> output CSV; run log and decisions.jsonl -> run_dir."""
    rows.sort(key=lambda r: case_sort_key(r[0]))
    input_order = [r["case_id"] for r in ctx.requests]
    if [r[0] for r in rows] != input_order:
        log("WARNING output order differs from input order (input not in numeric case order)")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(OUTPUT_HEADER)
        w.writerows(rows)

    log(f"done: {dict(ctx.stats)} -> {output_path}")
    (run_dir / "run_log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    with (run_dir / "decisions.jsonl").open("w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")


def load_frozen(path: Path) -> tuple[dict, str]:
    """Read a frozen config and verify it against the code. Returns (config, sha256 of the file)."""
    import hashlib
    import tomllib

    from audit import linking

    raw = path.read_bytes()
    cfg = tomllib.loads(raw.decode("utf-8"))
    problems = []
    for name, spec in cfg.get("prompts", {}).items():
        actual = hashlib.sha256((REPO_ROOT / spec["path"]).read_bytes()).hexdigest()
        if actual != spec["sha256"]:
            problems.append(f"prompt {name} ({spec['path']}) sha256 {actual[:12]} != frozen {spec['sha256'][:12]}")
    th = cfg.get("thresholds", {})
    checks = {"account_window_min": list(linking.ACCOUNT_WINDOW), "followup_window_min": list(linking.FOLLOWUP_WINDOW),
              "intent_batch_size": intent_mod.BATCH_SIZE,
              "result_ok": {k: sorted(v) for k, v in linking.RESULT_OK.items()}}
    for k, actual in checks.items():
        if k in th and th[k] != actual:
            problems.append(f"threshold {k}: code {actual} != frozen {th[k]}")
    if problems:
        raise SystemExit(f"frozen config {path} does not match the code:\n  " + "\n  ".join(problems))
    return cfg, hashlib.sha256(raw).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--requests", type=Path, default=DATASET_DIR / "deletion_requests.csv")
    ap.add_argument("--output", type=Path, default=REPO_ROOT / "output.csv")
    ap.add_argument("--dataset", type=Path, default=DATASET_DIR)
    ap.add_argument("--config", type=Path, default=FROZEN_DEFAULT)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--no-timing-links", action="store_true")
    ap.add_argument("--no-post-audit-links", action="store_true")
    ap.add_argument("--no-post-audit-unit-links", action="store_true")
    ap.add_argument("--no-unblocked-backup-rule", action="store_true")
    ap.add_argument("--strict-catalog-veto", action="store_true")
    ap.add_argument("--run-name", default="latest")
    ap.add_argument("--absence-rule", choices=("scope", "fallback", "fallback_delete", "override"), default=None)
    ap.add_argument("--no-verify-unobserved", action="store_true", help="disable fix 2")
    ap.add_argument("--no-llm", action="store_true", help="keyword intent fallback instead of the cached LLM")
    args = ap.parse_args()
    cfg, cfg_hash = load_frozen(args.config.resolve())
    pl = cfg["pipeline"]
    flags = {
        "timing_links": pl["timing_links"] and not args.no_timing_links,
        "post_audit_links": pl["post_audit_links"] and not args.no_post_audit_links,
        "post_audit_unit_links": pl["post_audit_unit_links"] and not args.no_post_audit_unit_links,
        "unblocked_backup_rule": pl["unblocked_backup_rule"] and not args.no_unblocked_backup_rule,
        "strict_catalog_veto": pl["strict_catalog_veto"] or args.strict_catalog_veto,
        "absence_rule": args.absence_rule or pl["absence_rule"],
        "verify_unobserved": pl["verify_unobserved"] and not args.no_verify_unobserved,
        "use_llm": pl["use_llm"] and not args.no_llm,
    }
    deviations = [f"{k}={v}" for k, v in flags.items() if v != pl[k]] + (["live API calls allowed"] if args.live else [])
    header = (f"config {args.config.name} sha256={cfg_hash}",
              "frozen run: cache-only" if not deviations else f"DEVIATES from frozen config: {', '.join(deviations)}")
    stats = run(args.requests.resolve(), args.output.resolve(), args.dataset.resolve(), flags["timing_links"],
                flags["post_audit_links"], WORK_DIR / "runs" / args.run_name,
                flags["absence_rule"], flags["verify_unobserved"], flags["use_llm"],
                flags["post_audit_unit_links"], flags["unblocked_backup_rule"], flags["strict_catalog_veto"],
                llm_config=args.config.resolve(), cache_only=not args.live, header=header)
    print(f"{header[0]}\n{header[1]}\n{args.output}: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
