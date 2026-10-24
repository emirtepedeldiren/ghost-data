"""PDF business documents: text layer via pypdf (no LLM).

A document is one unit. Each resource id printed in it is observed as present in the system that
owns the document (manifest `origin`), with the identifiers printed in the same document.
"""
from __future__ import annotations

from pathlib import Path

from audit import vocab
from audit.identity import extract_identifiers
from audit.model import Observation
from audit.parsers.common import resource_ids_in

ORIGIN_CATEGORY_HINT = {
    # document kind -> category of the resources it shows (the document itself is the evidence)
    "support_platform": None,          # attachment vs message decided by the resource's other observations
    "security_service": "SECURITY_AUDIT_LOG",
}


def extract_text(path: Path) -> str:
    from pypdf import PdfReader  # imported lazily so the rest of the pipeline runs without it

    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse(file_id: str, path: Path, captured_at: str, origin: str, unique_names: frozenset[str]):
    try:
        text = extract_text(path)
    except Exception as e:  # noqa: BLE001 - a broken PDF must not stop the run
        return [], [], [f"{file_id} {path.name}: PDF text extraction failed ({type(e).__name__})"]
    idents = frozenset(extract_identifiers(text, unique_names))
    rids = resource_ids_in(text)
    observations = []
    for rid in sorted(rids):
        system = vocab.system_for(rid, origin)
        if system is None:
            continue
        category = ORIGIN_CATEGORY_HINT.get(origin)
        if category is None and "attachment" in path.name.lower():
            category = "SUPPORT_ATTACHMENT"
        state = vocab.RETAINED if category in vocab.RETAINED_CATEGORIES else vocab.PRESENT
        observations.append(Observation(
            file_id=file_id, captured_at=captured_at, line_time=None, system_id=system, resource_id=rid,
            category=category, observed_state=state, identifiers_present=idents,
            linked_resources=frozenset(rids - {rid}), named_system=origin,
            locator=f"{path.name}", unit_kind="pdf:document",
        ))
    return observations, [], []
