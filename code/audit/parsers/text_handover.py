"""Plain-text handover records: `- <time> | <system> | <resource_id> | <CATEGORY> | <RESULT> | <subject>`.

Decisions: D9 (system as named in the line); explicit category and result columns.
"""
from __future__ import annotations

import re
from pathlib import Path

from audit import vocab
from audit.identity import extract_identifiers
from audit.model import Observation, ParseResult

LINE = re.compile(r"^\s*-\s*(\S+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(.*?)\s*$")


def parse(file_id: str, path: Path, captured_at: str, origin: str, unique_names: frozenset[str]) -> ParseResult:
    observations, warnings = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = LINE.match(line)
        if not m:
            if line.strip().startswith("-"):
                warnings.append(f"{file_id} {path.name}:{n}: unparsed record line")
            continue
        ts, named_system, rid, category, result, subject = m.groups()
        system = vocab.system_for(rid, named_system)
        if system is None:
            continue
        observations.append(Observation(
            file_id=file_id, captured_at=captured_at, line_time=ts, system_id=system, resource_id=rid,
            category=category, observed_state=vocab.state_from_result(category, result),
            identifiers_present=frozenset(extract_identifiers(subject)),
            named_system=named_system, locator=f"{path.name}:{n}", unit_kind=f"txt:lookup:{result}",
        ))
    return observations, [], warnings
