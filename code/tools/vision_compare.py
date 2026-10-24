"""D17: run two vision models on every PNG and score them against the LINK_RESOURCE_IDENTITY activities.

Usage: .venv/bin/python code/tools/vision_compare.py gemini-3.6-flash gemini-3.5-flash-lite
For each screenshot the matching catalog activity (resource_id + email + user_id) is a cross-check, not ground
truth: a correct reader of marketing_profile_console.png is expected to DISAGREE with activity_4008 (D13).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from audit.identity import EMAIL_RE, USER_RE  # noqa: E402
from audit.vision import extract, validate_record  # noqa: E402
from contract import DATASET_DIR  # noqa: E402
from llm import LLMClient, LLMError  # noqa: E402


def main(models: list[str]) -> int:
    manifest = [m for m in csv.DictReader((DATASET_DIR / "evidence_files.csv").open()) if m["file_path"].endswith(".png")]
    links = [a for a in csv.DictReader((DATASET_DIR / "system_activity.csv").open()) if a["event_type"] == "LINK_RESOURCE_IDENTITY"]
    report = {}
    for model in models:
        client = LLMClient()
        client.models["vision"] = model
        rows, totals = [], {"resource_found": 0, "identity_agrees": 0, "records": 0, "unparsed": 0, "errors": 0,
                            "input_tokens": 0, "output_tokens": 0}
        for m in manifest:
            act = next((a for a in links if a["event_time"][:13] == m["captured_at"][:13]), None)
            try:
                data = extract(client, DATASET_DIR / m["file_path"])
            except (LLMError, ValueError) as e:
                totals["errors"] += 1
                rows.append({"file_id": m["file_id"], "error": str(e)[:120]})
                continue
            recs, unparsed = [], []
            for r in data.get("records", []):
                clean, probs = validate_record(r)
                recs.append(clean)
                unparsed += probs
            totals["records"] += len(recs)
            totals["unparsed"] += len(unparsed)
            row = {"file_id": m["file_id"], "file": Path(m["file_path"]).name, "records": len(recs), "unparsed": unparsed}
            if act:
                want_email = {e.lower() for e in EMAIL_RE.findall(act["details"])}
                want_user = set(USER_RE.findall(act["details"]))
                hit = next((r for r in recs if r["resource_id"] == act["resource_id"]), None)
                row["activity"] = act["activity_id"]
                row["resource_found"] = hit is not None
                row["identity_agrees"] = bool(hit) and (hit["email"].lower() in want_email or hit["user_id"] in want_user
                                                         or any(r["email"].lower() in want_email or r["user_id"] in want_user for r in recs))
                totals["resource_found"] += row["resource_found"]
                totals["identity_agrees"] += row["identity_agrees"]
            row["extracted"] = [{k: r[k] for k in ("resource_id", "user_id", "name", "email", "status")} for r in recs]
            rows.append(row)
        usage = [json.loads(l) for l in (CODE / "cache" / "llm_calls.jsonl").open() if json.loads(l)["model"] == model and json.loads(l)["task"] == "vision"]
        totals["input_tokens"] = sum(u["input_tokens"] for u in usage)
        totals["output_tokens"] = sum(u["output_tokens"] for u in usage)
        report[model] = {"totals": totals, "files": rows}
        print(f"{model}: {json.dumps(totals)}")
        for row in rows:
            print(f"   {row['file_id']} {row.get('file', '')[:34]:<34} found={row.get('resource_found')} agrees={row.get('identity_agrees')} "
                  f"records={row.get('records')} unparsed={row.get('unparsed') or row.get('error', '')}")
    out = CODE / "cache" / "vision_compare.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"written {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["gemini-3.6-flash", "gemini-3.5-flash-lite"]))
