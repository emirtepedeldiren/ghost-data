"""Policy coverage audit over the eval outputs (Round 3). Read-only: no labels, no pipeline changes.

For every resource in the final run's decision log it reports the data category, the rule that fired
(or what replaced it), the branch of the rule's condition as far as the evidence captured <= audit_at
shows it (met / not met / undeterminable / n/a), and the operations written to output.csv.

Usage: python3 code/evaluation/policy_coverage.py [--run final] [--pred output.csv] [--md code/evaluation/policy_coverage.md]
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contract import DATASET_DIR, REPO_ROOT, WORK_DIR  # noqa: E402
from audit import vocab  # noqa: E402

CODE = REPO_ROOT / "code"
KV = re.compile(r'"?(\w+)"?\s*[=:]\s*("[^"]*"|[^\s,}]+)')
ORDER_CONTEXT = re.compile(r"\b(order|delivery|refund|invoice|agent|shipment|parcel|return|payment)\b", re.I)


# ---------------------------------------------------------------- raw evidence units per resource
def raw_units(manifest: list[dict]) -> dict[str, list[dict]]:
    """resource_id -> [{file_id, captured_at, fields, raw}] for every evidence unit that names it."""
    out: dict[str, list[dict]] = collections.defaultdict(list)

    def add(rid, f, fields, raw):
        out[rid].append({"file_id": f["file_id"], "captured_at": f["captured_at"], "fields": fields, "raw": raw})

    for f in manifest:
        path = DATASET_DIR / f["file_path"].removeprefix("dataset/").removeprefix("/")
        if not path.exists():
            path = REPO_ROOT / f["file_path"]
        suffix = path.suffix.lower()
        if suffix == ".json":
            def walk(o):
                if isinstance(o, dict):
                    if isinstance(o.get("resource_id"), str):
                        add(o["resource_id"], f, o, json.dumps(o, ensure_ascii=False))
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(json.loads(path.read_text(encoding="utf-8")))
        elif suffix == ".sql":
            db = sqlite3.connect(":memory:")
            db.executescript(path.read_text(encoding="utf-8"))
            for (table,) in db.execute("select name from sqlite_master where type='table'"):
                cur = db.execute(f'select * from "{table}"')
                cols = [c[0] for c in cur.description]
                if "resource_id" not in cols:
                    continue
                for row in cur:
                    rec = dict(zip(cols, row))
                    if "record_json" in rec:
                        try:
                            rec.update(json.loads(rec["record_json"]))
                        except (TypeError, ValueError):
                            pass
                    rec["_table"] = table
                    add(rec["resource_id"], f, rec, f"{table}: {json.dumps(rec, ensure_ascii=False)[:400]}")
        elif suffix in (".log", ".txt"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for m in vocab.RESOURCE_RE.finditer(line):
                    fields = {k: v.strip('"') for k, v in KV.findall(line)}
                    add(m.group(0), f, fields, f"{path.name}:{n}: {line.strip()[:400]}")
        elif suffix == ".pdf":
            from audit.parsers.pdf_text import extract_text
            text = extract_text(path)
            for m in vocab.RESOURCE_RE.finditer(text):
                window = text[m.start(): m.start() + 600]
                fields = {}
                ru = re.search(r"retain until\s+(\d{1,2} \w{3} \d{4})", window, re.I)
                if ru:
                    fields["retain_until_text"] = ru.group(1)
                add(m.group(0), f, fields, f"{path.name}: {' '.join(window.split())[:300]}")
        # PNG: the decision log's state source is used; raw vision output is not re-read here.
    return out


def parse_pdf_date(s: str) -> str:
    from datetime import datetime
    return datetime.strptime(s, "%d %b %Y").strftime("%Y-%m-%dT00:00:00Z")


# ---------------------------------------------------------------- condition evaluation per rule
def condition(category: str | None, res: dict, units: list[dict], audit_at: str, case: dict, resolver=None,
              any_time: bool = False) -> tuple[str, str]:
    """-> (branch, basis). branch in met / not met / undeterminable / n/a (Always rules, no rule).
    any_time=True also reads evidence captured after audit_at (a record's retention end date is a property of the record)."""
    before = units if any_time else [u for u in units if u["captured_at"] <= audit_at]
    fields = [u["fields"] for u in before]
    if category in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
        dates = []
        for fl in fields:
            for k in ("retention_until", "retained_until"):
                if fl.get(k):
                    dates.append((str(fl[k]), k))
            if fl.get("retain_until_text"):
                dates.append((parse_pdf_date(fl["retain_until_text"]), "pdf 'retain until'"))
        if not dates:
            return "undeterminable", "no retention/hold end date in evidence <= audit_at"
        latest = max(dates)
        return ("met" if latest[0] > audit_at else "not met"), f"{latest[1]}={latest[0]} vs audit_at {audit_at}"
    if category == "BACKUP_ARCHIVE":
        keys = ("restore_allowed", "eligible", "deletion_block", "decision")
        if any(k in fl for fl in fields for k in keys) or res.get("state") in (vocab.RESTORE_ALLOWED, vocab.RESTORE_BLOCKED) and res.get("state_source") == "observation" and "D22" not in " ".join(res.get("notes", [])):
            return "met", "a restore control (restore_allowed / eligible / deletion_block / decision / BLOCKED|ACTIVE) is observed"
        return "undeterminable", "no restore control observed <= audit_at"
    if category == "ANALYTICS_PROFILE":
        st = res.get("state")
        if st == vocab.PRESENT:
            return "met", "record exposes direct identifiers"
        if st == vocab.ANONYMIZED:
            return "not met", "record observed with all direct identifiers null"
        return "undeterminable", f"state={st}"
    if category == "SUPPORT_MESSAGE":
        texts = [str(fl.get(k)) for fl in fields for k in ("message_text", "message_excerpt", "subject", "body") if fl.get(k)]
        if not texts:
            return "undeterminable", "no conversation content in evidence <= audit_at"
        hit = next((ORDER_CONTEXT.search(t) for t in texts if ORDER_CONTEXT.search(t)), None)
        return (("met", f"content mentions '{hit.group(0)}'") if hit else ("undeterminable", "content present, no order/agent context word"))
    if category == "SUPPORT_ATTACHMENT":
        from audit.identity import extract_identifiers
        owners = set()
        for u in before:
            owners |= resolver.owners_of(extract_identifiers(u["raw"]))
        subject = case.get("subject", {}).get("user_id")
        if not owners:
            return "undeterminable", "no resolvable identifier on the attachment <= audit_at"
        if owners == {subject}:
            return "met", f"identifiers on the attachment resolve only to {subject}"
        return "not met", f"identifiers resolve to {sorted(owners)}"
    if category == "ACCOUNT_TOMBSTONE":
        others = case["resources"]
        acct_gone = any(r["resource_id"].startswith("account_") and r["state"] == vocab.ABSENT for r in others)
        retained = any(r["action"] == "RETAIN" for r in others)
        if acct_gone and retained:
            return "met", "subject's account ABSENT at audit_at and the case has retained records"
        return "undeterminable", f"account absent={acct_gone}, retained records={retained}"
    if category == "EMAIL_DELIVERY_LOG":
        has_email = any("@" in str(v) for fl in fields for v in fl.values())
        return ("met" if has_email else "undeterminable"), "email address in the log record" if has_email else "no record content"
    return "n/a", "rule condition is Always (or no rule)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="latest")
    ap.add_argument("--pred", type=Path, default=REPO_ROOT / "output.csv")
    ap.add_argument("--md", type=Path, default=CODE / "evaluation" / "policy_coverage.md")
    args = ap.parse_args()
    from audit.data import Dataset
    from audit.identity import IdentityResolver
    ds = Dataset.load(DATASET_DIR)
    resolver = IdentityResolver(ds.identities, ds.audit_log)
    rules = {r["data_category"]: r for r in csv.DictReader((DATASET_DIR / "deletion_rules.csv").open(encoding="utf-8"))}
    manifest = list(csv.DictReader((DATASET_DIR / "evidence_files.csv").open(encoding="utf-8")))
    units = raw_units(manifest)
    steps = {r["case_id"]: json.loads(r["steps_json"]) for r in csv.DictReader(args.pred.open(encoding="utf-8"))}
    rows = []
    for line in (WORK_DIR / "runs" / args.run / "decisions.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        for res in d.get("resources", []):
            cat = res["category"]
            rule = rules.get(cat or "")
            if res["category_source"].startswith("worker_hint"):
                fired = f"worker_hint→{res['action']}" + (f" (bypasses {rule['rule_id']}: {rule['required_action']})" if rule else "")
            elif cat == "DATA_FLOW":
                fired = "none (DATA_FLOW: no rule; code uses DISABLE_FLOW)"
            elif rule:
                fired = rule["rule_id"]
            else:
                fired = "none"
            u = units.get(res["resource_id"], [])
            branch, basis = condition(cat, res, u, d["audit_at"], d, resolver)
            later = condition(cat, res, u, d["audit_at"], d, resolver, any_time=True)[0] if branch == "undeterminable" else branch
            if rule and rule["condition"] == "Always":
                branch, basis = "n/a", "Always"
            ops = [s["operation"] for s in steps.get(d["case_id"], []) if s["resource_id"] == res["resource_id"]]
            rows.append({"case_id": d["case_id"], "audit_at": d["audit_at"], "resource_id": res["resource_id"], "category": cat,
                         "category_source": res["category_source"].split(":")[0], "fired": fired, "action": res["action"],
                         "state": res["state"], "evidence_id": res["evidence_id"], "branch": branch, "basis": basis, "later": later,
                         "ops": ops, "units": units.get(res["resource_id"], [])})

    # ---- coverage table
    cov = collections.defaultdict(lambda: {"n": 0, "fired": collections.Counter(), "branch": collections.Counter(), "later": collections.Counter(), "ops": collections.Counter()})
    for r in rows:
        c = cov[r["category"] or "(none)"]
        c["n"] += 1
        c["fired"][r["fired"]] += 1
        c["branch"][r["branch"]] += 1
        c["later"][r["later"]] += 1
        c["ops"][" + ".join(o.replace("REPORT_", "R:").replace("VERIFY_", "V:") for o in r["ops"]) or "(no step)"] += 1
    out = ["# Policy coverage audit (eval, final run)", "",
           f"Generated by `code/evaluation/policy_coverage.py` from `.work/runs/{args.run}/decisions.jsonl` and `{args.pred.name}`.",
           "Condition branches are evaluated from evidence captured `<= audit_at`; the pipeline itself does **not** evaluate them.", "",
           "## 1. Coverage per data category", "",
           "| category | resources | rule fired | condition branch (evidence <= audit_at) | branch incl. post-audit evidence | operations produced |",
           "|---|---|---|---|---|---|"]
    for cat in sorted(cov):
        c = cov[cat]
        fmt = lambda ctr: "<br>".join(f"{k}: {v}" for k, v in ctr.most_common())  # noqa: E731
        out.append(f"| {cat} | {c['n']} | {fmt(c['fired'])} | {fmt(c['branch'])} | {fmt(c['later'])} | {fmt(c['ops'])} |")
    for rid in sorted(rules, key=lambda k: rules[k]["rule_id"]):
        if rid not in cov:
            out.append(f"| {rid} | 0 | {rules[rid]['rule_id']} never fires: no linked resource of this category | | | |")

    # ---- examples: 2 per (category, branch), deterministic sample
    rng = random.Random(20261024)
    out += ["", "## 2. Audit examples (2 per category and branch; seeded random sample)", ""]
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["category"] or "(none)", r["branch"])].append(r)
    for key in sorted(groups):
        pick = sorted(rng.sample(groups[key], min(2, len(groups[key]))), key=lambda r: r["case_id"])
        out.append(f"### {key[0]} / {key[1]} ({len(groups[key])} resources)")
        for r in pick:
            ev = [u for u in r["units"] if u["file_id"] == r["evidence_id"]] or [u for u in r["units"] if u["captured_at"] <= r["audit_at"]] or r["units"]
            raw = ev[-1]["raw"] if ev else "(no text unit: screenshot or activity-only link)"
            src = f"{ev[-1]['file_id']} captured {ev[-1]['captured_at']}" if ev else "-"
            if ev and ev[-1]["captured_at"] > r["audit_at"]:
                src += " — POST-AUDIT, context only (state came from " + str(r["evidence_id"]) + ")"
            out += [f"- **{r['case_id']}** `{r['resource_id']}`. audit_at {r['audit_at']}. fired {r['fired']}; action {r['action']}; "
                    f"state {r['state']} ({r['evidence_id']}); basis: {r['basis']}",
                    f"  - evidence ({src}): `{raw.replace('`', chr(39))[:350]}`",
                    f"  - steps: {', '.join(r['ops']) or '(none)'}"]
        out.append("")

    # ---- undeterminable list
    und = [r for r in rows if r["branch"] == "undeterminable"]
    out += ["## 3. Conditions that could not be determined", "",
            f"{len(und)} resources in {len({r['case_id'] for r in und})} cases. In every one the code assumed the condition is **met** "
            "(the category's rule action was applied unchanged).", "",
            "| case | resource | category | basis | incl. post-audit evidence | action applied | steps |", "|---|---|---|---|---|---|---|"]
    for r in sorted(und, key=lambda r: (r["category"] or "", int(r["case_id"].split("_")[1]))):
        out.append(f"| {r['case_id']} | {r['resource_id']} | {r['category']} | {r['basis']} | {r['later']} | {r['action']} | {', '.join(r['ops']) or '(none)'} |")
    args.md.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out[: out.index("## 2. Audit examples (2 per category and branch; seeded random sample)")]))
    print(f"\nundeterminable: {len(und)} resources; by category:", dict(collections.Counter(r["category"] for r in und)))
    print(f"written {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
