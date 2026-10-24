"""Find dataset identifiers inside prompts (D31: prompts must not contain file-specific answers).

The reference set is every id (`<word>_<digits>`) and email that occurs in dataset/: the CSVs, the evidence text
files (logs, JSON, SQL, text), the PDF text layers, and the ids printed in the screenshots (as read by the cached
screenshot extractions in code/llm_cache/). A prompt "leaks" when it contains any member of that set.

Usage: python3 code/tools/prompt_leaks.py [PROMPT_FILE ...]    (default: code/prompts/*.md)
Exit status 1 when a leak is found.

Decisions: D31.
"""
from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from contract import DATASET_DIR  # noqa: E402

ID_RE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z_]*_\d+(?![0-9A-Za-z_])")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
TEXT_SUFFIXES = {".csv", ".log", ".json", ".sql", ".txt"}


def tokens(text: str) -> set[str]:
    return set(ID_RE.findall(text)) | {e.lower() for e in EMAIL_RE.findall(text)}


@lru_cache(maxsize=1)
def dataset_identifiers() -> frozenset[str]:
    found: set[str] = set()
    for p in DATASET_DIR.rglob("*"):
        if p.suffix in TEXT_SUFFIXES and p.is_file():
            found |= tokens(p.read_text(encoding="utf-8", errors="replace"))
        elif p.suffix == ".pdf":
            from audit.parsers.pdf_text import extract_text
            found |= tokens(extract_text(p))
    for f in (CODE / "llm_cache").glob("*.json"):   # screenshot extractions: ids printed in the PNGs
        try:
            data = json.loads(json.loads(f.read_text(encoding="utf-8"))["text"])
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(data, dict):
            found |= tokens(json.dumps(data.get("records", [])))
    return frozenset(found)


def leaks(text: str) -> list[str]:
    """Dataset identifiers that occur in the text, sorted."""
    return sorted(tokens(text) & dataset_identifiers())


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or sorted((CODE / "prompts").glob("*.md"))
    bad = 0
    for f in files:
        hits = leaks(f.read_text(encoding="utf-8"))
        bad += bool(hits)
        print(f"{'LEAK' if hits else 'clean'}  {f}  {hits}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
