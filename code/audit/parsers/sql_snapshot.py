"""SQLite-compatible snapshot exports, executed in an in-memory sqlite3 database.

Three layouts exist in the dataset:
  * inventory   - snapshot_scope(origin_system, captured_at) + typed tables per collection
  * replicas    - <x>_primary and <x>_<replica> tables with (resource_id, origin_system,
                  collection_name, record_json, updated_at)
  * typed pairs - customer_profiles_primary / customer_profiles_failover_replica, search_documents_primary
Every row in any table (primary or replica) is an observation, so a row that survives only in a replica
is still reported (replica drift). Each covered collection is also a Coverage snapshot.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from audit import vocab
from audit.model import Coverage, Observation
from audit.parsers.common import record_state, resource_ids_in, unit_identifiers

TABLE_SUFFIX = re.compile(r"_(primary|[a-z_]*replica[a-z_]*)$")


def base_collection(table: str) -> str:
    return TABLE_SUFFIX.sub("", table)


def _system_for_collection(collection: str) -> str | None:
    cat = vocab.COLLECTION_CATEGORY.get(collection)
    for prefix, c in vocab.PREFIX_CATEGORY.items():
        if c == cat and prefix != "object":
            return vocab.PREFIX_SYSTEM[prefix]
    if cat == "SUPPORT_ATTACHMENT":
        return "support_platform"
    return None


def parse(file_id: str, path: Path, captured_at: str, origin: str, unique_names: frozenset[str]):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(path.read_text(encoding="utf-8"))
    except sqlite3.Error as e:
        return [], [], [f"{file_id} {path.name}: sqlite error {e}"]
    db.row_factory = sqlite3.Row
    tables = [r[0] for r in db.execute("select name from sqlite_master where type='table' order by name")]

    scope: set[str] | None = None
    if "snapshot_scope" in tables:
        scope = {r["origin_system"] for r in db.execute("select origin_system from snapshot_scope")}

    observations: list[Observation] = []
    present: dict[tuple[str, str, str], set[str]] = {}   # (system, category, store) -> resource ids

    for table in tables:
        if table == "snapshot_scope":
            continue
        cols = [c[1] for c in db.execute(f'pragma table_info("{table}")')]
        for n, row in enumerate(db.execute(f'select * from "{table}"')):
            row = dict(row)
            if "record_json" in cols:
                try:
                    rec = json.loads(row["record_json"])
                except json.JSONDecodeError:
                    continue
                rec.setdefault("resource_id", row["resource_id"])
                collection = row.get("collection_name") or ""
                named_system = row.get("origin_system")
            else:
                rec = row
                collection = base_collection(table)
                named_system = _system_for_collection(collection)
            rid = rec.get("resource_id")
            if not isinstance(rid, str):
                continue
            category = vocab.COLLECTION_CATEGORY.get(collection)
            system = vocab.system_for(rid, named_system)
            if system is None:
                continue
            store = table if "replica" in table else ""
            flat = dict(rec)
            if isinstance(rec.get("document"), dict):
                flat.update({k: v for k, v in rec["document"].items() if k not in flat})
            observations.append(Observation(
                file_id=file_id, captured_at=captured_at, line_time=row.get("updated_at") or rec.get("indexed_at"),
                system_id=system, resource_id=rid, category=category,
                observed_state=record_state(category, flat),
                identifiers_present=frozenset(unit_identifiers(rec, unique_names)),
                linked_resources=frozenset(resource_ids_in(rec) - {rid}),
                named_system=named_system, locator=f"{path.name}:{table}[{n}]",
                unit_kind=f"sql:{'replica' if 'replica' in table else 'primary' if table.endswith('_primary') else 'table'}:{collection}",
                store=store,
            ))
            if category:
                present.setdefault((system, category, store), set()).add(rid)
        if "record_json" not in cols:
            collection = base_collection(table)
            category = vocab.COLLECTION_CATEGORY.get(collection)
            system = _system_for_collection(collection)
            if category and system and (scope is None or system in scope):
                # an empty typed table still covers its collection in its own store
                present.setdefault((system, category, table if "replica" in table else ""), set())

    coverage = [Coverage(file_id, captured_at, system, category, frozenset(ids), f"{path.name}:{store or 'primary'}:{category}", store)
                for (system, category, store), ids in sorted(present.items())]
    return observations, coverage, []
