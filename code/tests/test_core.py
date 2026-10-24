"""Unit tests for identity matching, parsing and ordering. Run: python3 -m unittest discover -s code/tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

from audit import vocab  # noqa: E402
from audit.identity import IdentityResolver, extract_identifiers, norm_email  # noqa: E402
from audit.intent import TEMPORARY_FALLBACK_LABEL, keyword_intent  # noqa: E402
from audit.parsers.logs import parse_line  # noqa: E402
from contract import case_sort_key, step_sort_key  # noqa: E402


class ResourceBoundaries(unittest.TestCase):
    def ids(self, text):
        return [m.group(0) for m in vocab.RESOURCE_RE.finditer(text)]

    def test_support_864_is_not_support_8642(self):
        line = "activity_960,UPDATE_RECORD,support_platform,search_service,support_8642,SUCCESS"
        self.assertEqual(self.ids(line), ["support_8642"])
        self.assertNotIn("support_864", self.ids(line))
        self.assertEqual(self.ids("resource_id=support_864 rows=1"), ["support_864"])

    def test_audit_record_prefix_and_filenames(self):
        self.assertEqual(self.ids("resource_id=audit_record_2016"), ["audit_record_2016"])
        self.assertEqual(self.ids("file=customer_export_1261.zip"), [])   # part of a file name, not export_1261
        self.assertEqual(self.ids("archive/customer-data/backup_7809.tar.zst"), ["backup_7809"])


class IdentifierMatching(unittest.TestCase):
    def test_user_ids_use_token_boundaries(self):
        self.assertEqual(extract_identifiers("subject_reference=user_13"), {"user:user_13"})
        self.assertNotIn("user:user_1", extract_identifiers("subject_reference=user_13"))

    def test_email_case_insensitive_and_near_miss(self):
        self.assertIn(norm_email("CHOI.JIHOON94@GMAIL.COM"), extract_identifiers("x choi.jihoon94@gmail.com y"))
        found = extract_identifiers("email=g.silva76.jr@gmail.com")
        self.assertNotIn(norm_email("g.silva76@gmail.com"), found)

    def test_phone_digits_normalized(self):
        self.assertEqual(extract_identifiers("+33 6 80 08 96 38"), {"phone:33680089638"})
        self.assertEqual(extract_identifiers("tel +82 10-5837-9451"), {"phone:821058379451"})

    def test_historical_email_resolves(self):
        identities = [{"user_id": "user_12", "name": "Min-jun Kim", "email": "minjun.kim@gmail.com",
                       "phone": "+82 10 2466 4678", "email_hash": "a" * 64, "updated_at": "2026-05-18T04:49:50Z"}]
        audit = [{"audit_id": "audit_282", "user_id": "user_12", "event_time": "2026-05-18T04:49:50Z",
                  "field_name": "email", "old_value": "minjun.kim90@gmail.com", "new_value": "minjun.kim@gmail.com"}]
        subject, how = IdentityResolver(identities, audit).resolve("MINJUN.KIM90@gmail.com")
        self.assertEqual((subject.user_id, how), ("user_12", "historical"))
        self.assertIn(norm_email("minjun.kim@gmail.com"), subject.identifiers)


class LogDialects(unittest.TestCase):
    def test_all_six_dialects(self):
        samples = {
            "kv": '2026-09-26T08:57:37Z level=INFO service=account_service trace_id=rb-1 event=profile_lookup_completed resource_id=account_7566 rows=0',
            "jsonl": '2026-09-16T08:57:34Z {"service":"account_service","trace_id":"tr-1","resource_id":"account_3960","event":"profile_lookup_completed","rows":"0"}',
            "syslog": '2026-08-22T10:51:17Z <15>1 node-1 account_service tr-4973247 - - level=DEBUG resource_id="account_1785" event="profile_lookup_completed" rows="1"',
            "cloud": '2026-09-30T11:51:03Z requestId=tr-2263120 level=DEBUG function=profile_service message="resource_id=\\"account_66777\\" event=\\"profile_lookup_completed\\" rows=\\"1\\""',
            "bracket": '2026-02-28T10:52:07Z [INFO] [export_service] [tr-7048906] export_download_check_completed: resource_id="export_9399" available="true"',
            "pipe": '2026-07-03T07:57:01Z | INFO | legal_service | tr-2637487 | retained_order_loaded | resource_id="legal_hold_4961" access="allowed"',
        }
        for dialect, line in samples.items():
            with self.subTest(dialect=dialect):
                ts, service, trace, event, fields, got = parse_line(line)
                self.assertEqual(got, dialect)
                self.assertTrue(event)
                self.assertTrue(vocab.resource_prefix(fields["resource_id"]))


class Ordering(unittest.TestCase):
    def test_numeric_case_order(self):
        self.assertEqual(sorted(["case_100", "case_33", "case_9"], key=case_sort_key), ["case_9", "case_33", "case_100"])

    def test_step_order_matches_sample_case_11(self):
        steps = [
            {"phase": "VERIFICATION", "operation": "VERIFY_RECORD_ABSENT", "system_id": "search_service", "resource_id": "search_052"},
            {"phase": "FINDING", "operation": "REPORT_EXPOSED_PII", "system_id": "support_platform", "resource_id": "support_864"},
            {"phase": "REMEDIATION", "operation": "DISABLE_DATA_FLOW", "system_id": "integration_service", "resource_id": "data_flow_9522"},
            {"phase": "VERIFICATION", "operation": "VERIFY_RECORD_ABSENT", "system_id": "account_service", "resource_id": "account_4248"},
            {"phase": "FINDING", "operation": "REPORT_EXPOSED_PII", "system_id": "analytics_platform", "resource_id": "analytics_2034"},
        ]
        got = [s["resource_id"] for s in sorted(steps, key=step_sort_key)]
        self.assertEqual(got, ["analytics_2034", "support_864", "data_flow_9522", "account_4248", "search_052"])


class TemporaryIntentFallback(unittest.TestCase):
    def test_label_and_basic_negation(self):
        self.assertIn("TEMPORARY", TEMPORARY_FALLBACK_LABEL)
        self.assertTrue(keyword_intent("Please permanently delete the account associated with this email address.").authorizes_deletion)
        self.assertFalse(keyword_intent("Please remove me from marketing lists only. I still want receipts.").authorizes_deletion)
        self.assertFalse(keyword_intent("I still use it, so please don't delete or change anything.").authorizes_deletion)


if __name__ == "__main__":
    unittest.main()


def _users():
    from audit.identity import IdentityResolver
    rows = [{"user_id": f"user_{i}", "name": f"Person {i}", "email": f"p{i}@example.com", "phone": f"+1 555 000 00{i}",
             "email_hash": str(i) * 64, "updated_at": "2026-01-01T00:00:00Z"} for i in (1, 2, 3)]
    return IdentityResolver(rows, [])


def _obs(rid, file_id="evidence_01", captured="2026-09-10T00:00:00Z", state=None, idents=()):
    from audit.model import Observation
    return Observation(file_id, captured, None, vocab.system_for(rid), rid, None, state, frozenset(idents))


def _req(cid, user, status="COMPLETE", reported="2026-09-02T12:00:00Z"):
    return {"case_id": cid, "email": f"p{user}@example.com", "requested_at": "2026-09-01T12:00:00Z",
            "reported_status": status, "reported_at": reported, "audit_at": "2026-09-15T12:00:00Z", "request_text": "delete"}


def _worker(aid, t, etype, rid, result="SUCCESS"):
    return {"activity_id": aid, "event_time": t, "event_type": etype, "source_system": "deletion_worker",
            "target_system": "", "resource_id": rid, "reported_result": result, "details": f"Policy action completed for {rid}."}


class TimingLinkerL2(unittest.TestCase):
    """D20: global unique assignment with the one-resource-per-type slot rule."""

    def link(self, requests, activity, observations):
        from audit.linking import OwnershipIndex, TimingLinker
        res = _users()
        t = TimingLinker(requests, activity, OwnershipIndex(observations, res), res)
        return {cid: sorted(l.resource_id for l in ls) for cid, ls in t.assigned.items()}, t.ambiguous

    def test_slot_rule_and_elimination(self):
        reqs = [_req("case_1", 1), _req("case_2", 2)]
        acts = [_worker("a1", "2026-09-02T11:00:00Z", "DELETE_RECORD", "account_10"),
                _worker("a2", "2026-09-02T11:05:00Z", "DELETE_RECORD", "account_20"),
                _worker("a3", "2026-09-02T11:31:00Z", "ANONYMIZE_RECORD", "analytics_20")]
        obs = [_obs("account_10", idents={"email:p1@example.com"}), _obs("analytics_10", idents={"user:user_1"})]
        assigned, _ = self.link(reqs, acts, obs)
        self.assertEqual(assigned["case_2"], ["account_20", "analytics_20"])   # user_1 already owns both types
        self.assertEqual(assigned["case_1"], ["account_10"])

    def test_symmetric_followups_stay_unlinked(self):
        reqs = [_req("case_1", 1), _req("case_2", 2)]
        acts = [_worker("a1", "2026-09-02T11:31:09Z", "ANONYMIZE_RECORD", "analytics_4574"),
                _worker("a2", "2026-09-02T11:31:53Z", "ANONYMIZE_RECORD", "analytics_6077")]
        assigned, ambiguous = self.link(reqs, acts, [])
        self.assertEqual(assigned, {})
        self.assertEqual(len(ambiguous["case_1"]), 2)

    def test_requestless_owner_and_windows(self):
        reqs = [_req("case_1", 1, status="NOT_STARTED")]
        acts = [_worker("a1", "2026-09-02T11:31:00Z", "ANONYMIZE_RECORD", "analytics_31"),        # owned by user_3
                _worker("a2", "2026-09-02T10:30:00Z", "DELETE_RECORD", "account_30", "FAILED"),     # -90 min: outside
                _worker("a3", "2026-09-02T11:10:00Z", "DELETE_RECORD", "account_40", "SUCCESS")]    # result mismatch
        assigned, _ = self.link(reqs, acts, [_obs("analytics_31", idents={"user:user_3"})])
        self.assertEqual(assigned, {})


class StateAndCatalogRules(unittest.TestCase):
    def test_d22_unblocked_backup_is_restore_risk(self):
        from audit.model import EvidenceIndex
        from audit.reconstruct import StateIndex
        rules = [{"data_category": "BACKUP_ARCHIVE", "required_action": "RETAIN"}]
        subj = frozenset({"email:p1@example.com"})
        held = _obs("backup_9287", idents=subj)
        si = StateIndex(EvidenceIndex(observations=[held]), [], rules)
        self.assertEqual(si.resolve("backup_9287", "2026-09-15T00:00:00Z", "fallback_delete", subj).state, vocab.RESTORE_ALLOWED)
        self.assertIsNone(si.resolve("backup_9287", "2026-09-15T00:00:00Z", "fallback_delete", frozenset({"email:x@y.z"})).state)
        blocked = _obs("backup_9287", "evidence_02", "2026-09-12T00:00:00Z", vocab.RESTORE_BLOCKED)
        si = StateIndex(EvidenceIndex(observations=[held, blocked]), [], rules)
        self.assertEqual(si.resolve("backup_9287", "2026-09-15T00:00:00Z", "fallback_delete", subj).state, vocab.RESTORE_BLOCKED)

    def test_d13_revised_veto_only_for_known_other_user(self):
        from audit.linking import OwnershipIndex, catalog_links
        res = _users()
        subject, _ = res.resolve("p1@example.com")
        act = [{"activity_id": "activity_4008", "event_time": "2026-09-10T00:00:00Z", "event_type": "LINK_RESOURCE_IDENTITY",
                "source_system": "data_catalog", "target_system": "", "resource_id": "marketing_001", "reported_result": "SUCCESS",
                "details": "Data catalog linked marketing_001 to p1@example.com and user_1."}]
        unknown = {"marketing_001": [_obs("marketing_001", idents={"email:someone@else.example"})]}
        links, rejected, flags = catalog_links(subject, "2026-09-15T00:00:00Z", act, OwnershipIndex([], res), unknown)
        self.assertEqual(([l.resource_id for l in links], rejected, len(flags)), (["marketing_001"], [], 1))
        known = {"marketing_001": [_obs("marketing_001", idents={"email:p2@example.com"})]}
        links, rejected, _ = catalog_links(subject, "2026-09-15T00:00:00Z", act, OwnershipIndex([], res), known)
        self.assertEqual((links, len(rejected)), ([], 1))
