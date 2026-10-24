"""Classify every unique request_text (eval + sample files) once, in deterministic batches, filling the LLM cache.

Usage: .venv/bin/python code/tools/warm_intent_cache.py
Later pipeline runs then read intent from the cache (deterministic, no API calls).
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from audit.intent import IntentClassifier  # noqa: E402
from contract import DATASET_DIR  # noqa: E402
from llm import LLMClient  # noqa: E402


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    texts: dict[str, list[str]] = {}
    for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
        with (DATASET_DIR / name).open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                texts.setdefault(r["request_text"], []).append(r["case_id"])
    clf = IntentClassifier(LLMClient())
    for line in clf.prepare(texts):
        print(line)
    results = [clf.classify(t) for t in texts]
    sources = Counter(r.source.split(":")[0] for r in results)
    print(f"unique texts={len(texts)} sources={dict(sources)}")
    for text, r in zip(texts, results):
        if not r.authorizes_deletion or not r.source.startswith("llm"):
            print(f"  {texts[text][0]:<9} deletion={r.authorizes_deletion} {r.source} | {r.rationale[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
