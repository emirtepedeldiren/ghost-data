"""Evidence parsing: dispatch every manifest entry to its format parser and build one EvidenceIndex."""
from __future__ import annotations

from pathlib import Path

from audit.model import EvidenceIndex
from audit.parsers import json_records, logs, pdf_text, png_vision, sql_snapshot, text_handover

PARSERS = {
    ".json": json_records.parse,
    ".log": logs.parse,
    ".sql": sql_snapshot.parse,
    ".txt": text_handover.parse,
    ".pdf": pdf_text.parse,
    ".png": png_vision.parse,
}


def build_index(manifest: list[dict], dataset_dir: Path, unique_names: frozenset[str]) -> EvidenceIndex:
    index = EvidenceIndex()
    for entry in manifest:
        path = dataset_dir / entry["file_path"]
        parser = PARSERS.get(path.suffix.lower())
        if parser is None:
            index.warnings.append(f"{entry['file_id']} {path.name}: no parser for {path.suffix}")
            continue
        if not path.exists():
            index.warnings.append(f"{entry['file_id']} {path.name}: file missing")
            continue
        obs, cov, warns = parser(entry["file_id"], path, entry["captured_at"], entry["origin"], unique_names)
        index.observations.extend(obs)
        index.coverage.extend(cov)
        for w in warns:
            (index.pending if png_vision.VISION_PENDING in w else index.warnings).append(w)
    return index
