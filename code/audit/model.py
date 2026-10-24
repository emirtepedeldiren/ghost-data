"""Normalized records shared by parsers, linking, state reconstruction and policy."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Observation:
    """One resource observed in one evidence unit (log trace group, JSON object, SQL row, text line, document).

    Field names follow the user's spec: (file_id, captured_at, line_time, system_id, resource_id,
    category, observed_state, identifiers_present).
    """
    file_id: str
    captured_at: str
    line_time: str | None              # timestamp carried by the unit itself, if any
    system_id: str
    resource_id: str
    category: str | None               # explicit category from the evidence, None when not stated
    observed_state: str | None         # vocab.STATES, None when the unit only links (no state claim)
    identifiers_present: frozenset[str]  # normalized identifiers found in the same unit (see identity.py)
    linked_resources: frozenset[str] = frozenset()  # other resource ids in the same unit (e.g. source_resource_id)
    named_system: str | None = None    # system as literally named by the evidence (for the decision log)
    locator: str = ""                  # path:line or table:row, for traceability
    unit_kind: str = ""                # parser family + event / collection
    store: str = ""                    # "" = the system's primary store; else a named copy (e.g. a replica table)
    retention_until: str | None = None  # retention / hold end stated by the unit (ISO-8601), D23


@dataclass(frozen=True)
class Coverage:
    """A snapshot that lists a whole collection: absence of a resource here proves ABSENT (CONTRACT.md §6)."""
    file_id: str
    captured_at: str
    system_id: str
    category: str                      # data category of the covered collection (USER_PROFILE, ...)
    present: frozenset[str]            # resource ids present in this snapshot of this store
    locator: str = ""
    store: str = ""                    # "" = primary store; replica snapshots name their table


# What every evidence parser returns: (observations, coverage snapshots, warnings).
ParseResult = tuple[list["Observation"], list["Coverage"], list[str]]


@dataclass
class EvidenceIndex:
    observations: list[Observation] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)   # e.g. PNGs awaiting vision extraction
