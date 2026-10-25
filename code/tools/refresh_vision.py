"""D31: re-extract the 11 screenshots with the neutral prompt, then switch the frozen config only if output.csv is unchanged.

Usage (one command, after the free-tier quota reset):
    .venv/bin/python code/tools/refresh_vision.py
Dry run without network (proves the comparison and both branches; run it in a copy of the repo, it edits files):
    .venv/bin/python code/tools/refresh_vision.py --mock identical
    .venv/bin/python code/tools/refresh_vision.py --mock different

Steps:
  0. preflight: frozen config still points at the superseded prompt; the new prompt is leak-free; every document anchor the
     success branch edits exists. Nothing has been called or changed yet.
  a. time: refuses before the next 07:00 UTC reset after the last production screenshot call (skipped with --mock).
  b. re-extract the 11 PNGs with code/prompts/vision_extract.md into a staging cache (.work/vision_refresh/cache). The old
     cache entries in code/llm_cache/ are not touched. Responses already staged by an earlier (interrupted) run are reused
     without a call (D34 resume). LLM_ATTEMPTS is forced to 1, so every counted call is exactly one HTTP request; at most
     --max-calls of them (default 11), each with a flushed progress line before and after it.
  c. compare old vs new extractions field by field per screenshot; print the differences.
  d. run the pipeline cache-only with a candidate config (new prompt; old + staged cache) and diff output.csv against the
     current one (its sha256 must stay 774aa9ca...).
  e. identical: copy the staged entries into code/llm_cache/, repoint frozen_v1.toml to the new prompt, delete the
     superseded prompt, move the old screenshot calls to development in the usage data, regenerate usage_report.md,
     update hashes and the D31 passages in the docs, run the frozen pipeline + validator + all tests, rebuild code.zip,
     print READY TO SUBMIT.
  f. different: change nothing in the repo, print the row-level output diff and the extraction diffs, exit 2 (user decision;
     the decided fallback is to keep the current output and the documented limitation).

Decisions: D31 (approved), D34 (resume, one HTTP request per counted call, at most 11; supersedes D31's cap of 12),
D26 (the result stays cache-only).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os

os.environ["LLM_ATTEMPTS"] = "1"   # D34: no silent provider retries; must be set before llm.load_dotenv (setdefault) runs

CODE = Path(__file__).resolve().parents[1]
ROOT = CODE.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "tools"))

FROZEN = CODE / "config" / "frozen_v1.toml"
NEW_PROMPT = CODE / "prompts" / "vision_extract.md"
CACHE = CODE / "llm_cache"
STAGE = ROOT / ".work" / "vision_refresh"
OUTPUT = ROOT / "output.csv"
EXPECTED_OUTPUT_SHA = "774aa9ca3e0073383fc9251ce17217b684c4b06161c5385c712945a420d1cb1f"
FIELDS = ("resource_id", "user_id", "name", "email", "phone", "status")
DOCS_WITH_CONFIG_HASH = [ROOT / "README.md", CODE / "CONTRACT.md", CODE / "evaluation" / "evaluation_report.md"]
REPORT = CODE / "evaluation" / "evaluation_report.md"
MARKERS = ("D31:limitation", "D31:compliance", "D31:cost")
README = ROOT / "README.md"
README_MARKER = "D31:readme"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def say(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- config helpers
VISION_LINE = re.compile(r'^vision = \{ path = "([^"]+)", sha256 = "([0-9a-f]{64})" \}$', re.M)


def frozen_vision_entry(text: str) -> tuple[str, str]:
    m = VISION_LINE.search(text)
    if not m:
        raise SystemExit("preflight: no vision prompt entry in frozen_v1.toml")
    return m.group(1), m.group(2)


def config_with_new_prompt(text: str, cache_dir: str | None = None, cache_only: bool | None = None, clean: bool = False) -> str:
    rel = NEW_PROMPT.relative_to(ROOT).as_posix()
    text = VISION_LINE.sub(f'vision = {{ path = "{rel}", sha256 = "{sha(NEW_PROMPT)}" }}', text)
    if clean:   # drop the D31 pending comment lines
        text = "\n".join(line for line in text.splitlines() if not line.startswith(("# D31:", "# code/tools/refresh_vision.py"))) + "\n"
    if cache_dir is not None:
        text = re.sub(r'^cache_dir = ".*"$', f'cache_dir = "{cache_dir}"', text, flags=re.M)
    if cache_only is not None:
        text = re.sub(r"^cache_only = \w+$", f"cache_only = {'true' if cache_only else 'false'}", text, flags=re.M)
    return text


# ---------------------------------------------------------------- mock provider (no network)
def install_mock(mode: str, old_texts: dict[str, str]) -> None:
    """Replace the Gemini provider by one that answers from the old extractions (identical) or perturbs one (different)."""
    import llm

    class MockProvider(llm._Provider):
        name = "gemini"

        def call(self, model, system, prompt, images, json_schema, temperature):
            text = old_texts[images[0].name]
            if mode == "different" and images[0].name.startswith("search_index_inspector"):
                data = json.loads(text)
                data["records"] = [r for r in data["records"] if r.get("resource_id") != "search_" + "428"]   # drop one printed record
                text = json.dumps(data)
            return text, 1400, 500, {"mock": True}

    llm.PROVIDERS["gemini"] = MockProvider


# ---------------------------------------------------------------- comparison
def record_key(r: dict, i: int) -> str:
    return r.get("resource_id") or r.get("user_id") or f"#{i}"


def compare(old: dict, new: dict) -> list[str]:
    diffs = []
    for k in ("application", "page_title"):
        if old.get(k) != new.get(k):
            diffs.append(f"{k}: {old.get(k)!r} -> {new.get(k)!r}")
    o = {record_key(r, i): r for i, r in enumerate(old.get("records", []))}
    n = {record_key(r, i): r for i, r in enumerate(new.get("records", []))}
    for k in sorted(set(o) - set(n)):
        diffs.append(f"record {k}: removed")
    for k in sorted(set(n) - set(o)):
        diffs.append(f"record {k}: added")
    for k in sorted(set(o) & set(n)):
        for f in FIELDS:
            if (o[k].get(f) or "") != (n[k].get(f) or ""):
                diffs.append(f"record {k}.{f}: {o[k].get(f)!r} -> {n[k].get(f)!r}")
        oa = {a.get("key"): a.get("value") for a in o[k].get("attributes") or []}
        na = {a.get("key"): a.get("value") for a in n[k].get("attributes") or []}
        for a in sorted(set(oa) | set(na), key=str):
            if oa.get(a) != na.get(a):
                diffs.append(f"record {k}.attributes[{a}]: {oa.get(a)!r} -> {na.get(a)!r}")
    return diffs


def row_diff(before: Path, after: Path) -> list[str]:
    a = {r["case_id"]: r for r in csv.DictReader(before.open(encoding="utf-8"))}
    b = {r["case_id"]: r for r in csv.DictReader(after.open(encoding="utf-8"))}
    out = []
    for cid in a:
        if a[cid] != b.get(cid):
            sa, sb = json.loads(a[cid]["steps_json"]), json.loads(b[cid]["steps_json"]) if cid in b else []
            key = lambda s: (s["phase"], s["operation"], s["system_id"], s["resource_id"], s["state"], s["evidence_id"])  # noqa: E731
            ka, kb = set(map(key, sa)), set(map(key, sb))
            out.append(f"{cid}: {a[cid]['deletion_status']} -> {b.get(cid, {}).get('deletion_status')}; "
                       f"removed {sorted(ka - kb)}; added {sorted(kb - ka)}")
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    """Run the D31 refresh; returns 0 (switched, READY TO SUBMIT), 2 (output would change, nothing switched).

    Steps (see the module docstring): 0 preflight; a time gate; b re-extract into a staging cache within the call
    budget; c field-by-field comparison; d candidate pipeline run and output diff; e switch config, cache, usage data
    and docs, then verify (frozen run, validator, tests, zip); f print the diffs and stop.
    """
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", choices=("identical", "different"), help="no network: answer from the old extractions")
    ap.add_argument("--max-calls", type=int, default=11, help="HTTP requests allowed in this run (D34)")
    args = ap.parse_args()
    import llm
    from audit import vision
    import prompt_leaks

    # 0. preflight ------------------------------------------------------------------------------------------
    frozen_text = FROZEN.read_text(encoding="utf-8")
    legacy_rel, legacy_sha = frozen_vision_entry(frozen_text)
    legacy = ROOT / legacy_rel
    if "legacy" not in Path(legacy_rel).parts:
        raise SystemExit(f"preflight: frozen_v1.toml already uses {legacy_rel}; nothing to refresh")
    if sha(legacy) != legacy_sha:
        raise SystemExit("preflight: the superseded prompt does not match its pinned hash")
    if prompt_leaks.leaks(NEW_PROMPT.read_text(encoding="utf-8")):
        raise SystemExit(f"preflight: new prompt still contains dataset ids {prompt_leaks.leaks(NEW_PROMPT.read_text())}")
    if sha(OUTPUT) != EXPECTED_OUTPUT_SHA:
        raise SystemExit(f"preflight: output.csv sha256 is {sha(OUTPUT)[:12]}..., expected {EXPECTED_OUTPUT_SHA[:12]}...")
    report = REPORT.read_text(encoding="utf-8")
    for m in MARKERS:
        if f"<!-- {m} -->" not in report or f"<!-- /{m} -->" not in report:
            raise SystemExit(f"preflight: marker {m} missing in evaluation_report.md")
    readme = README.read_text(encoding="utf-8")
    if f"<!-- {README_MARKER} -->" not in readme or f"<!-- /{README_MARKER} -->" not in readme:
        raise SystemExit(f"preflight: marker {README_MARKER} missing in README.md")
    config_hash = sha(FROZEN)
    for d in DOCS_WITH_CONFIG_HASH:
        if config_hash not in d.read_text(encoding="utf-8"):
            raise SystemExit(f"preflight: {d.name} does not quote the current config sha256 {config_hash[:12]}...")
    calls = [json.loads(line) for line in (CACHE / "calls.jsonl").open(encoding="utf-8")]
    old_vision_calls = [c for c in calls if c["purpose"] == "production" and c["task"] == "vision"]
    pngs = sorted((ROOT / "dataset" / "evidence" / "images").glob("*.png"))
    if len(old_vision_calls) != len(pngs):
        raise SystemExit(f"preflight: {len(old_vision_calls)} production screenshot calls for {len(pngs)} PNGs")
    say(f"preflight OK: {len(pngs)} screenshots; superseded prompt {legacy_rel}; new prompt sha256 {sha(NEW_PROMPT)[:12]}...")

    # a. time ------------------------------------------------------------------------------------------------
    last = max(c["created_ts"] for c in old_vision_calls)
    last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    reset = (last_dt + timedelta(days=1)).replace(hour=7, minute=0, second=0) if last_dt.hour >= 7 else last_dt.replace(hour=7, minute=0, second=0)
    now = datetime.now(timezone.utc)
    if args.mock:
        say(f"a. time check skipped (--mock); a live run needs now >= {reset:%Y-%m-%dT%H:%M}Z")
    elif now < reset:
        raise SystemExit(f"a. refusing: it is {now:%Y-%m-%dT%H:%M}Z; the free-tier quota resets at {reset:%Y-%m-%dT%H:%M}Z")
    else:
        say(f"a. time OK: {now:%Y-%m-%dT%H:%M}Z >= reset {reset:%Y-%m-%dT%H:%M}Z")

    # b. re-extract into a staging cache ------------------------------------------------------------------------
    # D34 resume: keep the responses staged by an earlier run (their cache keys embed the new prompt); clear the rest.
    if STAGE.exists():
        for f in STAGE.iterdir():
            if f.name != "cache":
                shutil.rmtree(f) if f.is_dir() else f.unlink()
    (STAGE / "cache").mkdir(parents=True, exist_ok=True)
    stage_cfg = STAGE / "config_stage.toml"
    stage_cfg.write_text(config_with_new_prompt(frozen_text, cache_dir=(STAGE / "cache").relative_to(ROOT).as_posix(),
                                                 cache_only=False), encoding="utf-8")
    old_client = llm.LLMClient(FROZEN, cache_only=True)          # reads the old entries (superseded prompt)
    old = {p.name: vision.extract(old_client, p) for p in pngs}
    old_texts = {p.name: json.dumps({k: v for k, v in old[p.name].items() if not k.startswith("_")}) for p in pngs}
    if args.mock:
        install_mock(args.mock, old_texts)
    client = llm.LLMClient(stage_cfg, cache_only=False)
    assert vision.prompt_text(client) == NEW_PROMPT.read_text(encoding="utf-8")
    provider = client.provider
    if not args.mock and getattr(provider, "attempts", None) != 1:
        raise SystemExit(f"b. refusing: provider makes {getattr(provider, 'attempts', '?')} HTTP attempts per call, D34 requires 1")
    counter = {"made": 0, "label": ""}
    real_call = provider.call

    def counted_call(*a, **kw):   # D34: one counted call = one HTTP request, announced before and after
        if counter["made"] >= args.max_calls:
            raise SystemExit(f"b. call budget of {args.max_calls} exhausted; staged entries kept in {STAGE}, repo unchanged")
        counter["made"] += 1
        n, t0 = counter["made"], time.time()
        say(f"b. {counter['label']}: call {n}/{args.max_calls} sent {now_utc()} (timeout {getattr(provider, 'timeout', 0):.0f}s)")
        try:
            out = real_call(*a, **kw)
        except Exception as e:
            say(f"b. {counter['label']}: call {n} FAILED after {time.time() - t0:.1f}s ({str(e)[:160]})")
            raise
        say(f"b. {counter['label']}: call {n} ok after {time.time() - t0:.1f}s ({out[1]} in / {out[2]} out tokens)")
        return out

    provider.call = counted_call
    vision_key = lambda p: client._cache_key(client.models["vision"], "vision", vision.prompt_text(client),  # noqa: E731
                                             "Transcribe the customer records in this screenshot.", [p], vision.VISION_SCHEMA)
    new, new_calls, reused = {}, [], 0
    for i, p in enumerate(pngs, 1):
        for attempt in (1, 2):
            counter["label"] = f"[{i}/{len(pngs)}] {p.name}"
            staged = STAGE / "cache" / f"{vision_key(p)}.json"
            ts = (datetime.fromtimestamp(staged.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                  if staged.exists() else now_utc())
            try:
                new[p.name] = vision.extract(client, p)
                if new[p.name]["_cached"]:
                    reused += 1
                    say(f"b. {counter['label']}: reused staged response from {ts} (no call)")
                break
            except (llm.LLMError, ValueError) as e:
                say(f"b. {counter['label']}: attempt {attempt} failed ({str(e)[:100]})")
                staged.unlink(missing_ok=True)   # an unparsable answer must not be served on retry
                if attempt == 2:
                    raise SystemExit(f"b. {p.name} failed twice; repo unchanged") from None
                if not args.mock:
                    time.sleep(5)
        key = vision_key(p)
        entry = json.loads((STAGE / "cache" / f"{key}.json").read_text(encoding="utf-8"))
        new_calls.append({"cache_key": key, "purpose": "production", "provider": client.provider_name,
                          "model": client.models["vision"], "task": "vision", "image": p.name, "created_ts": ts,
                          "input_tokens": entry["input_tokens"], "output_tokens": entry["output_tokens"]})
    made = counter["made"]
    say(f"b. re-extracted {len(new)} screenshots: {reused} reused from staging, {made} {'mock' if args.mock else 'HTTP'} "
        f"call(s) in this run; old cache untouched")

    # c. compare extractions --------------------------------------------------------------------------------------
    ex_diffs = {p.name: compare(old[p.name], new[p.name]) for p in pngs}
    say(f"c. extraction differences: {sum(map(len, ex_diffs.values()))} field(s) in {sum(1 for v in ex_diffs.values() if v)} screenshot(s)")
    for name, ds in ex_diffs.items():
        for d in ds:
            say(f"   {name}: {d}")

    # d. pipeline with the candidate config -------------------------------------------------------------------------
    merged = STAGE / "merged_cache"
    shutil.copytree(CACHE, merged)
    for f in (STAGE / "cache").glob("*.json"):
        shutil.copy2(f, merged / f.name)
    cand_cfg = STAGE / "config_candidate.toml"
    cand_cfg.write_text(config_with_new_prompt(frozen_text, cache_dir=merged.relative_to(ROOT).as_posix(), cache_only=True),
                        encoding="utf-8")
    import main as pipeline
    cand_out = STAGE / "output_candidate.csv"
    pipeline.run(ROOT / "dataset" / "deletion_requests.csv", cand_out, ROOT / "dataset", True, True, STAGE / "run",
                 llm_config=cand_cfg, cache_only=True)
    identical = cand_out.read_bytes() == OUTPUT.read_bytes()
    say(f"d. candidate output.csv sha256 {sha(cand_out)[:12]}... vs current {sha(OUTPUT)[:12]}...: "
        f"{'IDENTICAL' if identical else 'DIFFERENT'}")

    # f. different: stop -------------------------------------------------------------------------------------------
    if not identical:
        say("f. output.csv would change. Nothing in the repo was modified (staged files are in .work/vision_refresh/).")
        for line in row_diff(OUTPUT, cand_out):
            say(f"   ROW {line}")
        say("   Decision needed. Decided fallback (D31): keep the current output.csv and the documented limitation.")
        return 2

    # e. identical: switch -------------------------------------------------------------------------------------------
    for f in (STAGE / "cache").glob("*.json"):
        shutil.copy2(f, CACHE / f.name)
    FROZEN.write_text(config_with_new_prompt(frozen_text, clean=True), encoding="utf-8")
    shutil.rmtree(legacy.parent) if legacy.parent.name == "legacy" else legacy.unlink()
    dev = [json.loads(line) for line in (CACHE / "dev_calls.jsonl").open(encoding="utf-8")]
    for c in old_vision_calls:
        dev.append({"purpose": "development", "provider": c["provider"], "model": c["model"], "task": c["task"],
                    "created_ts": c["created_ts"], "input_tokens": c["input_tokens"], "output_tokens": c["output_tokens"],
                    "note": "screenshots with the superseded prompt, replaced by D31"})
    (CACHE / "dev_calls.jsonl").write_text("".join(json.dumps(d, sort_keys=True) + "\n" for d in dev), encoding="utf-8")
    kept = [c for c in calls if c not in old_vision_calls] + new_calls
    kept.sort(key=lambda r: (r["purpose"] != "production", r["created_ts"], r["cache_key"]))
    (CACHE / "calls.jsonl").write_text("".join(json.dumps(c, sort_keys=True) + "\n" for c in kept), encoding="utf-8")
    run = lambda *cmd: subprocess.run([sys.executable, *cmd], cwd=ROOT, capture_output=True, text=True)  # noqa: E731
    r = run("code/tools/usage_report.py")
    if r.returncode:
        raise SystemExit(f"e. usage_report failed:\n{r.stderr[-800:]}")
    # docs: config hash, D31 passages, cost line
    new_hash = sha(FROZEN)
    for d in DOCS_WITH_CONFIG_HASH:
        d.write_text(d.read_text(encoding="utf-8").replace(config_hash, new_hash), encoding="utf-8")
    prod = [c for c in kept if c["purpose"] == "production"]
    prices = __import__("tomllib").loads((CODE / "config.toml").read_text(encoding="utf-8"))["llm"]["prices"]
    tok = sum(c["input_tokens"] + c["output_tokens"] for c in prod)
    usd = sum(c["input_tokens"] * prices[c["model"]]["input"] / 1e6 + c["output_tokens"] * prices[c["model"]]["output"] / 1e6 for c in prod)
    n_req = sum(1 for _ in (ROOT / "dataset" / "deletion_requests.csv").open(encoding="utf-8")) - 1
    n_diff = sum(map(len, ex_diffs.values()))
    blocks = {
        "D31:limitation": (
            f"- **Screenshot prompt replaced (D31, fixed {now_utc()[:10]}).** The prompt that originally produced the screenshot responses gave\n"
            "  example ids that are printed in the dataset's screenshots. It was replaced by a neutral prompt (`code/prompts/vision_extract.md`,\n"
            f"  id formats as patterns only) and the 11 screenshots were re-extracted: {n_diff} extracted field(s) changed, and `output.csv`\n"
            "  is byte-identical. The 11 superseded calls are reported as development calls in `usage_report.md`; no prompt contains a\n"
            "  dataset id or email (`PromptLeaks` test)."),
        "D31:compliance": "Prompts contain no id or email that occurs in `dataset/` (`PromptLeaks` test, including the prompts pinned in the frozen config; fixed by D31)",
        "D31:cost": (f"**Cost:** {len(prod)} LLM calls, {tok:,} tokens, est. **${usd:.4f} total, ${usd / n_req:.6f} per request** at list price. "
                     "The final run was served from the\nresponse cache with 0 live calls (`usage_report.md`)."),
    }
    text = REPORT.read_text(encoding="utf-8")
    for m, body in blocks.items():
        block = f"<!-- {m} -->\n{body}\n<!-- /{m} -->" if m == "D31:limitation" else f"<!-- {m} -->{body}<!-- /{m} -->"
        text = re.sub(rf"<!-- {m} -->.*?<!-- /{m} -->", lambda _, block=block: block, text, flags=re.S)
    REPORT.write_text(text, encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")
    readme_block = (f"<!-- {README_MARKER} -->\n- **Screenshot prompt replaced (D31).** The screenshots were re-extracted with the neutral prompt\n"
                    f"  (`code/prompts/vision_extract.md`); `output.csv` is byte-identical and no prompt contains a dataset id.\n<!-- /{README_MARKER} -->")
    README.write_text(re.sub(rf"<!-- {README_MARKER} -->.*?<!-- /{README_MARKER} -->", lambda _: readme_block, readme_text, flags=re.S),
                      encoding="utf-8")
    with (CODE / "CONTRACT.md").open("a", encoding="utf-8") as f:
        f.write(f"- **D31 done ({now_utc()}, D34 resume):** screenshots re-extracted with the neutral prompt ({reused} staged + {made} "
                f"call(s) in the resumed run); {n_diff} extracted "
                f"field(s) changed; output.csv byte-identical; frozen_v1.toml repointed to `code/prompts/vision_extract.md` "
                f"(config sha256 `{new_hash}`); superseded prompt deleted.\n")
    # verify: frozen run (cache-only), validator, tests, zip
    r = run("code/main.py", "--run-name", "frozen_v1")
    if r.returncode or OUTPUT.read_bytes() != cand_out.read_bytes():
        raise SystemExit(f"e. frozen run failed or changed output.csv:\n{r.stdout[-400:]}{r.stderr[-800:]}")
    say(f"e. frozen run: {r.stdout.strip().splitlines()[0]}")
    for cmd in (("code/validate.py", "--output", "output.csv"), ("-m", "unittest", "discover", "-s", "code/tests")):
        r = run(*cmd)
        if r.returncode:
            raise SystemExit(f"e. {' '.join(cmd)} FAILED:\n{r.stdout[-800:]}{r.stderr[-1500:]}")
        say(f"e. {' '.join(cmd)}: {(r.stdout.strip() or r.stderr.strip()).splitlines()[-1]}")
    (ROOT / "code.zip").unlink(missing_ok=True)
    z = subprocess.run(["zip", "-qr", "code.zip", "README.md", "code", "-x", "*/__pycache__/*", "*/.venv/*", "*/venv/*",
                        "*/node_modules/*", "*/.git/*", "*/.DS_Store"], cwd=ROOT, capture_output=True, text=True)
    if z.returncode:
        raise SystemExit(f"e. zip failed: {z.stderr}")
    say(f"e. code.zip rebuilt ({(ROOT / 'code.zip').stat().st_size:,} bytes); config sha256 {new_hash[:12]}...; "
        f"output.csv sha256 {sha(OUTPUT)[:12]}...")
    say("READY TO SUBMIT")
    return 0


if __name__ == "__main__":
    t0 = time.time()
    code = main()
    print(f"({time.time() - t0:.0f}s)")
    sys.exit(code)
