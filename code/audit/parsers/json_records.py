"""JSON evidence: snapshot bundles, service exports, record-lookup manifests and reports.

Every dict carrying a `resource_id` is one unit. Its meaning comes from where it sits:
  * lookup record   - has record_type + result (manifests, ledgers, operational_record_history)
  * collection row  - inside {collection, records:[...]} (bundles, reports); the collection is also
                      a Coverage snapshot (absence there proves ABSENT)
  * service export  - inside service_exports.<system>.<kind>[] with its own observed_at
"""
from __future__ import annotations

import json
from pathlib import Path

from audit import vocab
from audit.model import Coverage, Observation
from audit.parsers.common import is_null, record_state, resource_ids_in, retention_end, unit_identifiers

SERVICE_EXPORT_CATEGORY = {
    "profile_lookups": "USER_PROFILE",
    "profiles": "ANALYTICS_PROFILE",
    "restore_entries": "BACKUP_ARCHIVE",
    "exports": "USER_EXPORT",
    "connectors": "DATA_FLOW",
    "index_queries": "SEARCH_INDEX_ENTRY",
    "audit_records": "SECURITY_AUDIT_LOG",
    "tickets": "SUPPORT_MESSAGE",
}
CATEGORY_PREFIX = {v: k for k, v in vocab.PREFIX_CATEGORY.items()}
CATEGORY_PREFIX["SUPPORT_ATTACHMENT"] = "support"   # ticket attachments use support_ ids


def _obs(file_id, captured_at, line_time, rec, named_system, category, state, unique_names, locator, kind):
    rid = rec["resource_id"]
    system = vocab.system_for(rid, named_system)
    if system is None:
        return None
    return Observation(
        file_id=file_id, captured_at=captured_at, line_time=line_time, system_id=system, resource_id=rid,
        category=category, observed_state=state,
        identifiers_present=frozenset(unit_identifiers(rec, unique_names)),
        linked_resources=frozenset(resource_ids_in(rec) - {rid}),
        named_system=named_system, locator=locator, unit_kind=kind, retention_until=retention_end(rec),
    )


def parse(file_id: str, path: Path, captured_at: str, origin: str, unique_names: frozenset[str]):
    data = json.loads(path.read_text(encoding="utf-8"))
    observations: list[Observation] = []
    coverage: list[Coverage] = []

    def handle_record(rec: dict, ctx: dict, loc: str):
        if "record_type" in rec and "result" in rec:
            cat = rec["record_type"]
            state = vocab.state_from_result(cat, rec["result"])
            o = _obs(file_id, captured_at, rec.get("recorded_at"), rec, rec.get("system"), cat, state,
                     unique_names, loc, f"json:lookup:{rec['result']}")
        elif ctx.get("service_kind"):
            kind = ctx["service_kind"]
            cat = SERVICE_EXPORT_CATEGORY.get(kind)
            if kind == "profile_lookups":
                state = vocab.ABSENT if (rec.get("status_code") == 404 or is_null(rec.get("profile"))) else vocab.PRESENT
            elif kind == "index_queries":
                hits = rec.get("hits") or {}
                total = hits.get("total") if isinstance(hits, dict) else None
                state = vocab.PRESENT if (total or (isinstance(hits, dict) and hits.get("documents"))) else vocab.ABSENT
            else:
                flat = dict(rec)
                if isinstance(rec.get("profile"), dict):
                    flat.update(rec["profile"])
                state = record_state(cat, flat)
            o = _obs(file_id, captured_at, rec.get("observed_at"), rec, ctx.get("service"), cat, state,
                     unique_names, loc, f"json:service_export:{kind}")
        elif ctx.get("collection"):
            coll = ctx["collection"]
            cat = vocab.COLLECTION_CATEGORY.get(coll)
            flat = dict(rec)
            if isinstance(rec.get("document"), dict):   # search documents nest their identifiers
                flat.update({k: v for k, v in rec["document"].items() if k not in flat})
            o = _obs(file_id, captured_at, ctx.get("snapshot_time"), rec, ctx.get("data_store"), cat,
                     record_state(cat, flat), unique_names, loc, f"json:collection:{coll}")
        else:
            o = None
        if o:
            observations.append(o)

    def walk(o, ctx: dict, loc: str):
        if isinstance(o, dict):
            if "resource_id" in o and isinstance(o["resource_id"], str):
                handle_record(o, ctx, loc)
                return  # nested documents belong to this unit
            # {collection, records:[...]} container -> coverage + rows
            if "collection" in o and isinstance(o.get("records"), list):
                coll = o["collection"]
                cat = vocab.COLLECTION_CATEGORY.get(coll)
                sub = {**ctx, "collection": coll, "data_store": o.get("data_store") or ctx.get("data_store"),
                       "snapshot_time": o.get("captured_at") or ctx.get("snapshot_time")}
                prefix = CATEGORY_PREFIX.get(cat) if cat else None
                if cat and prefix:
                    system = sub["data_store"] if sub["data_store"] in vocab.CANONICAL_SYSTEMS else vocab.PREFIX_SYSTEM[prefix]
                    present = frozenset(r["resource_id"] for r in o["records"] if isinstance(r, dict) and "resource_id" in r)
                    coverage.append(Coverage(file_id, captured_at, system, cat, present, f"{loc}.{coll}"))
                for i, r in enumerate(o["records"]):
                    walk(r, sub, f"{loc}.{coll}[{i}]")
                return
            for k, v in o.items():
                sub = ctx
                if k == "service_exports":
                    sub = {**ctx, "in_service_exports": True}
                elif ctx.get("in_service_exports") and not ctx.get("service"):
                    sub = {**ctx, "service": k}
                elif ctx.get("service") and isinstance(v, list):
                    sub = {**ctx, "service_kind": k}
                walk(v, sub, f"{loc}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, ctx, f"{loc}[{i}]")

    walk(data, {}, path.name)
    return observations, coverage, []
