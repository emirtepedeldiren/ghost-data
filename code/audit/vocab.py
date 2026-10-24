"""Domain vocabulary: systems, resource prefixes, categories and normalized states.

Everything here is derived from participant-facing files (deletion_rules.csv categories,
resource-id prefixes and system names that appear in the evidence). No case data.
"""
from __future__ import annotations

import re

# Canonical system for each resource-id prefix (CONTRACT.md §7.1). Used when the evidence
# does not name one of these canonical systems itself (D9).
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "support": "support_platform",
    "analytics": "analytics_platform",
    "audit_record": "security_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "export": "export_service",
    "tombstone": "identity_service",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "marketing": "marketing_platform",
    "legal_hold": "legal_service",
    "object": "object_storage",
}
CANONICAL_SYSTEMS = frozenset(PREFIX_SYSTEM.values())

RESOURCE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(sorted(PREFIX_SYSTEM, key=len, reverse=True)) + r")_(\d+)(?![0-9A-Za-z_])"
)


def resource_prefix(resource_id: str) -> str | None:
    m = RESOURCE_RE.fullmatch(resource_id)
    return m.group(1) if m else None


def system_for(resource_id: str, named_system: str | None = None) -> str | None:
    """D9: the system named in the evidence when it is canonical, else the prefix map."""
    if named_system in CANONICAL_SYSTEMS:
        return named_system
    prefix = resource_prefix(resource_id)
    return PREFIX_SYSTEM.get(prefix) if prefix else None


# Default data category per prefix (last resort of D5).
PREFIX_CATEGORY = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "support": "SUPPORT_MESSAGE",
    "analytics": "ANALYTICS_PROFILE",
    "audit_record": "SECURITY_AUDIT_LOG",
    "data_flow": "DATA_FLOW",
    "backup": "BACKUP_ARCHIVE",
    "export": "USER_EXPORT",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "cache": "CACHE_ENTRY",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "marketing": "MARKETING_PROFILE",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "object": "SUPPORT_ATTACHMENT",
}

# Explicit category from a collection / table name (D5 rule 1).
COLLECTION_CATEGORY = {
    "customer_profiles": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "support_conversations": "SUPPORT_MESSAGE",
    "support_records": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_export_jobs": "USER_EXPORT",
    "customer_exports": "USER_EXPORT",
    "connector_configurations": "DATA_FLOW",
    "connector_subscriptions": "DATA_FLOW",
    "backup_catalog": "BACKUP_ARCHIVE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "retained_order_records": "LEGAL_HOLD_RECORD",
}

# Normalized observed states (CONTRACT.md §6).
PRESENT = "PRESENT_IDENTIFIABLE"   # record exists and still exposes the subject
ABSENT = "ABSENT"
ANONYMIZED = "ANONYMIZED"
REDACTED = "REDACTED"
FLOW_ACTIVE = "FLOW_ACTIVE"
FLOW_DISABLED = "FLOW_DISABLED"
RESTORE_ALLOWED = "RESTORE_ALLOWED"
RESTORE_BLOCKED = "RESTORE_BLOCKED"
RETAINED = "RETAINED"
STATES = frozenset({PRESENT, ABSENT, ANONYMIZED, REDACTED, FLOW_ACTIVE, FLOW_DISABLED,
                    RESTORE_ALLOWED, RESTORE_BLOCKED, RETAINED})

# Result vocabulary of record-lookup manifests (handover txt, *_diagnostic logs, ledgers, histories).
RESULT_STATE = {
    "NOT_FOUND": ABSENT,
    "IDENTIFIABLE": PRESENT,
    "ANONYMIZED": ANONYMIZED,
    "REDACTED": REDACTED,
    "ACTIVE": None,      # depends on category: data flow vs backup
    "DISABLED": FLOW_DISABLED,
    "BLOCKED": RESTORE_BLOCKED,
    "FOUND": None,       # depends on category: retained vs remaining
}

RETAINED_CATEGORIES = frozenset({"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"})
# D23: categories whose RETAIN rule is time-bound (rule_12 "within the supplied retention period",
# rule_13 "a supplied legal hold is active"). Not met -> DELETE.
TIME_BOUND_RETAIN = RETAINED_CATEGORIES


def state_from_result(category: str | None, result: str) -> str | None:
    result = (result or "").upper()
    if result == "ACTIVE":
        return RESTORE_ALLOWED if category == "BACKUP_ARCHIVE" else FLOW_ACTIVE
    if result == "FOUND":
        return RETAINED if category in RETAINED_CATEGORIES else PRESENT
    return RESULT_STATE.get(result)
