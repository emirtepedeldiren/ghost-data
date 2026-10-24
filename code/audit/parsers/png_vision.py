"""Screenshots (PNG). STUB until the vision model is approved (D2 / D10).

Returns no observations and reports the file as pending, so the run log shows exactly which
evidence was not read. The real implementation will call the provider-agnostic LLM client,
cache the structured extraction per file_id, and validate every id against vocab.RESOURCE_RE.
"""
from __future__ import annotations

from pathlib import Path

VISION_PENDING = "vision pending"


def parse(file_id: str, path: Path, captured_at: str, origin: str, unique_names: frozenset[str]):
    return [], [], [f"{file_id} {path.name}: {VISION_PENDING} (no observations extracted)"]
