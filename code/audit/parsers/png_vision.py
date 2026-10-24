"""Screenshots (PNG) via the vision model (D2, D17).

Without an LLM client (e.g. --no-llm) the parser returns no observations and reports the file as
"vision pending". With a client, the extraction is cached per image, every id is validated
(audit.vision.validate_record), and each record with a valid resource_id becomes one Observation.

Decisions: D2, D17 (model choice), D31 (prompt path from the frozen config).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from audit import vocab
from audit.model import Observation, ParseResult
from audit.vision import extract, record_identifiers, record_state, validate_record

if TYPE_CHECKING:
    from llm import LLMClient

VISION_PENDING = "vision pending"


def parse(file_id: str, path: Path, captured_at: str, origin: str, unique_names: frozenset[str],
          client: LLMClient | None = None) -> ParseResult:
    if client is None:
        return [], [], [f"{file_id} {path.name}: {VISION_PENDING} (no observations extracted)"]
    from llm import LLMError

    try:
        data = extract(client, path)
    except (LLMError, ValueError) as e:
        return [], [], [f"{file_id} {path.name}: vision extraction failed ({str(e)[:120]})"]
    observations, warnings = [], []
    records = []
    for r in data.get("records", []):
        clean, problems = validate_record(r)
        warnings += [f"{file_id} {path.name}: {p} (flagged, not used)" for p in problems]
        records.append(clean)
    for r in records:
        rid = r["resource_id"]
        if not rid:
            continue
        system = vocab.system_for(rid, origin)
        if system is None:
            continue
        idents = record_identifiers(r, unique_names)
        observations.append(Observation(
            file_id=file_id, captured_at=captured_at, line_time=None, system_id=system, resource_id=rid,
            category=None, observed_state=record_state(rid, r, idents), identifiers_present=frozenset(idents),
            linked_resources=frozenset(), named_system=origin, locator=f"{path.name}#{rid}",
            unit_kind=f"png:{data.get('_model', '?')}",
        ))
    return observations, [], warnings
