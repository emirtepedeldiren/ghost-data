"""Log evidence in six dialects, normalized to (time, service, trace_id, event, fields).

Dialects (all observed in dataset/evidence/logs):
  kv       2026-..Z level=INFO service=x trace_id=t event=e k=v k="v"
  jsonl    2026-..Z {"service":..,"trace_id":..,"event":..,...}
  syslog   2026-..Z <15>1 host service trace - - k="v" ...
  cloud    2026-..Z requestId=t level=.. function=service message="k=\"v\" ..."
  bracket  2026-..Z [LEVEL] [service] [trace] event: k="v" ...
  pipe     2026-..Z | LEVEL | service | trace | event | k="v" ...
Lines sharing a trace id form one unit (a lookup_started line carries the key, the completed line
carries the resource and the result). Lines without a trace id are units on their own.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path

from audit import vocab
from audit.model import Observation, ParseResult
from audit.parsers.common import record_state, resource_ids_in, retention_end, truthy, unit_identifiers

KV = re.compile(r'(\w+)=("((?:[^"\\]|\\.)*)"|\S+)')
TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)$")
SYSLOG = re.compile(r"^<\d+>1 \S+ (\S+) (\S+) - - (.*)$")
CLOUD = re.compile(r'^requestId=(\S+) level=\S+ function=(\S+) message="(.*)"$')
BRACKET = re.compile(r"^\[(\w+)\] \[(\S+)\] \[(\S+)\] (\w+): ?(.*)$")
PIPE = re.compile(r"^\| (\w+) \| (\S+) \| (\S+) \| (\w+) \| ?(.*)$")
TRACE_LIKE = re.compile(r"^(tr|rb)-[\w-]+$")


def kv(s: str) -> dict:
    return {m.group(1): (m.group(3).replace('\\"', '"') if m.group(3) is not None else m.group(2))
            for m in KV.finditer(s)}


def parse_line(line: str) -> tuple[str, str | None, str | None, str | None, dict, str] | None:
    """-> (ts, service, trace_id, event, fields, dialect) or None."""
    m = TS.match(line.rstrip("\n"))
    if not m:
        return None
    ts, rest = m.groups()
    if rest.startswith("{"):
        try:
            d = json.loads(rest)
        except json.JSONDecodeError:
            return None
        return ts, d.get("service") or d.get("logger"), d.get("trace_id"), d.get("event"), d, "jsonl"
    if (m := SYSLOG.match(rest)):
        d = kv(m.group(3))
        return ts, m.group(1), m.group(2), d.get("event"), d, "syslog"
    if (m := CLOUD.match(rest)):
        d = kv(m.group(3).replace('\\"', '"'))
        return ts, m.group(2), m.group(1), d.get("event"), d, "cloud"
    if (m := BRACKET.match(rest)):
        d = kv(m.group(5))
        return ts, m.group(2), m.group(3), m.group(4), d, "bracket"
    if (m := PIPE.match(rest)):
        d = kv(m.group(5))
        return ts, m.group(2), m.group(3), m.group(4), d, "pipe"
    d = kv(rest)
    return ts, d.get("service"), d.get("trace_id"), d.get("event"), d, "kv"


def _n(v) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


# event -> (category, state function). category None = the log does not state a data category.
def _profile(f):
    return vocab.ABSENT if (_n(f.get("rows")) == 0 or str(f.get("http_status")) == "404") else vocab.PRESENT


EVENT_RULES = {
    "profile_lookup_completed": ("USER_PROFILE", _profile),
    "index_query_completed": ("SEARCH_INDEX_ENTRY", lambda f: vocab.PRESENT if str(f.get("result")).lower() == "hit" else vocab.ABSENT),
    "analytics_profile_read_completed": ("ANALYTICS_PROFILE", lambda f: record_state("ANALYTICS_PROFILE", f)),
    "analytics_profile_read": ("ANALYTICS_PROFILE", lambda f: vocab.ANONYMIZED if _n(f.get("direct_identifiers")) == 0 else vocab.PRESENT),
    "analytics_identity_scan": ("ANALYTICS_PROFILE", lambda f: vocab.PRESENT if _n(f.get("direct_identifiers")) > 0 else vocab.ANONYMIZED),
    "contact_lookup_completed": (None, lambda f: vocab.PRESENT if _n(f.get("rows")) > 0 else vocab.ABSENT),
    "support_ticket_read_completed": ("SUPPORT_MESSAGE", lambda f: record_state("SUPPORT_MESSAGE", f)),
    "message_pii_scan": ("SUPPORT_MESSAGE", lambda f: vocab.PRESENT),
    "message_read": ("SUPPORT_MESSAGE", lambda f: record_state("SUPPORT_MESSAGE", f)),
    "attachment_pii_scan": ("SUPPORT_ATTACHMENT", lambda f: vocab.PRESENT),
    "audit_record_read_completed": ("SECURITY_AUDIT_LOG", lambda f: vocab.RETAINED if _n(f.get("rows", 1)) > 0 else vocab.ABSENT),
    "connector_config_read_completed": ("DATA_FLOW", lambda f: vocab.FLOW_ACTIVE if truthy(f.get("enabled")) else vocab.FLOW_DISABLED),
    "connector_config_loaded": ("DATA_FLOW", lambda f: vocab.FLOW_ACTIVE if truthy(f.get("enabled")) else vocab.FLOW_DISABLED),
    "connector_heartbeat": ("DATA_FLOW", lambda f: vocab.FLOW_ACTIVE if truthy(f.get("enabled")) else vocab.FLOW_DISABLED),
    "restore_policy_read_completed": ("BACKUP_ARCHIVE", lambda f: vocab.RESTORE_ALLOWED if truthy(f.get("restore_allowed")) else vocab.RESTORE_BLOCKED),
    "restore_candidate_evaluated": ("BACKUP_ARCHIVE", lambda f: vocab.RESTORE_ALLOWED
                                    if truthy(f.get("eligible")) and not truthy(f.get("deletion_block")) else vocab.RESTORE_BLOCKED),
    "restore_authorization_completed": ("BACKUP_ARCHIVE", lambda f: vocab.RESTORE_BLOCKED
                                        if str(f.get("decision")).lower() == "deny" else vocab.RESTORE_ALLOWED),
    "export_download_check_completed": ("USER_EXPORT", lambda f: vocab.PRESENT if truthy(f.get("available")) else vocab.ABSENT),
    "export_metadata_read_completed": ("USER_EXPORT", lambda f: vocab.PRESENT if truthy(f.get("available")) else vocab.ABSENT),
    "retained_order_loaded": ("LEGAL_HOLD_RECORD", lambda f: vocab.RETAINED),
    "contact_query_completed": (None, lambda f: vocab.PRESENT if _n(f.get("rows")) > 0 else vocab.ABSENT),
    # record_lookup lines state system, record_type and result explicitly (handled in parse()).
    "record_lookup": (None, None),
}


def parse(file_id: str, path: Path, captured_at: str, origin: str, unique_names: frozenset[str]) -> ParseResult:
    groups: "OrderedDict[str, list[tuple]]" = OrderedDict()
    warnings: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parsed = parse_line(line)
        if parsed is None or not parsed[3]:
            warnings.append(f"{file_id} {path.name}:{n}: unparsed log line")
            continue
        ts, service, trace, event, fields, dialect = parsed
        key = f"{trace}" if trace and TRACE_LIKE.match(str(trace)) else f"line{n}"
        groups.setdefault(key, []).append((n, ts, service, event, fields, dialect))

    observations: list[Observation] = []
    for key, lines in groups.items():
        all_fields = [ln[4] for ln in lines]
        idents = frozenset(unit_identifiers(all_fields, unique_names))
        rids_in_unit = set()
        for f in all_fields:
            rid = f.get("resource_id")
            if isinstance(rid, str) and vocab.resource_prefix(rid):
                rids_in_unit.add(rid)
        linked = resource_ids_in(all_fields)
        for rid in sorted(rids_in_unit):
            # The state-bearing line for this resource: last line naming it with a known event rule.
            state_line = None
            for ln in lines:
                if ln[4].get("resource_id") == rid and ln[3] in EVENT_RULES:
                    state_line = ln
            n, ts, service, event, fields, dialect = state_line or next(ln for ln in lines if ln[4].get("resource_id") == rid)
            named = fields.get("system") if fields.get("system") in vocab.CANONICAL_SYSTEMS else service
            system = vocab.system_for(rid, named)
            if system is None:
                continue
            if event == "record_lookup":
                category = fields.get("record_type")
                state = vocab.state_from_result(category, fields.get("result"))
            elif state_line is not None:
                category, fn = EVENT_RULES[event]
                state = fn(fields)
            else:
                category, state = None, None
            observations.append(Observation(
                file_id=file_id, captured_at=captured_at, line_time=ts, system_id=system, resource_id=rid,
                category=category, observed_state=state, identifiers_present=idents,
                linked_resources=frozenset(linked - {rid}), named_system=named,
                locator=f"{path.name}:{n}", unit_kind=f"log:{dialect}:{event}",
                retention_until=next((t for f in all_fields if f.get("resource_id") == rid for t in [retention_end(f)] if t), None),
            ))
    return observations, [], warnings

