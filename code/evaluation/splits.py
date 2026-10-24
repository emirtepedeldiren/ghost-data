"""D16 holdout split, chosen mechanically before any tuning.

Holdout = every 4th sample case in numeric case_id order (positions 4, 8, ..., 32).
Rule: per-case diffs of holdout cases are never printed or inspected; only aggregate scores.

Decisions: D16.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contract import DATASET_DIR, case_sort_key


def sample_case_ids(path: Path = DATASET_DIR / "sample_output.csv") -> list[str]:
    with path.open(newline="", encoding="utf-8") as f:
        return sorted((r["case_id"] for r in csv.DictReader(f)), key=case_sort_key)


def holdout_ids(ids: list[str] | None = None) -> list[str]:
    ids = ids if ids is not None else sample_case_ids()
    return [ids[i] for i in range(3, len(ids), 4)]


def tuning_ids(ids: list[str] | None = None) -> list[str]:
    ids = ids if ids is not None else sample_case_ids()
    hold = set(holdout_ids(ids))
    return [c for c in ids if c not in hold]


if __name__ == "__main__":
    print("holdout:", holdout_ids())
    print("tuning :", len(tuning_ids()), "cases")
