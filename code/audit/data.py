"""Loading of the participant-facing CSV files under dataset/."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@dataclass
class Dataset:
    dataset_dir: Path
    identities: list[dict]
    audit_log: list[dict]
    activity: list[dict]
    rules: list[dict]
    manifest: list[dict]

    @classmethod
    def load(cls, dataset_dir: Path) -> "Dataset":
        return cls(
            dataset_dir=dataset_dir,
            identities=read_csv(dataset_dir / "user_identities.csv"),
            audit_log=read_csv(dataset_dir / "user_audit_log.csv"),
            activity=read_csv(dataset_dir / "system_activity.csv"),
            rules=read_csv(dataset_dir / "deletion_rules.csv"),
            manifest=read_csv(dataset_dir / "evidence_files.csv"),
        )

    def all_requests(self) -> list[dict]:
        """Every request row in the participant files (eval + sample). Used only to disambiguate
        the timing module's one-to-one assignment; never as labels."""
        rows = []
        for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
            p = self.dataset_dir / name
            if p.exists():
                rows.extend(read_csv(p))
        return rows
