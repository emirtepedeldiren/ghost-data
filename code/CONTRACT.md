# CONTRACT.md — Ghost Data Still Exists deletion-audit agent

Status: **v0.5** (2026-10-24, Round 1 closed). Built: `code/contract.py`, `code/validate.py`, `code/evaluation/main.py`,
the deterministic core `code/audit/` + `code/main.py`, the provider-agnostic client `code/llm/` (not yet wired in),
`code/tools/list_models.py`, unit tests `code/tests/`. Decisions D1–D15 are the user's.

**Core principle (D11 revised):** an activity row may *propose* a resource-to-user link. **State and citations come only
from observed evidence files.** When an observation contradicts an activity claim, the observation wins. D13 and D14
still hold: marketing_001 is contradicted by its screenshot, and object_19 has no observed state.
**D15:** the timing module is a normal link source under D11. It stays behind `--timing-links`; its default is decided by
the sample comparison (§15).

Precedence when sources disagree on the output format: `problem_statement.md` > `README.md` > `AGENTS.md` (D6).
Precedence for packaging paths: `README.md` (D8).

---

## 1. Scope

For each row of `dataset/deletion_requests.csv` (518 rows) produce one row of `output.csv`:
`case_id,deletion_status,steps_json`. Only participant-facing files under `dataset/` are read. No labels,
no case-family shortcuts, no organizer-only data. `dataset/sample_*` files are used **only** for evaluation.

## 2. Time rule (hard invariant)

For a case with cutoff `T = audit_at`:

| Source | Usable for state / citation iff |
|---|---|
| evidence file | `captured_at <= T` (inclusive; proven by case_11 citing evidence_03 captured exactly at `T`) |
| `system_activity` row | `event_time <= T` |
| `user_audit_log` row | `event_time <= T` |
| screenshot / PDF extraction | inherits the file's `captured_at` |

A file captured after `T` is **never cited** and never sets state.

**D12, identity linking from post-T files.** A file with `captured_at > T` may be used to decide *who a resource belongs
to*, never *what state it is in*. Ownership does not change over time; state does. Sample case_09 needs this: its
`account_3960` and `audit_record_4902` are only linked in evidence_06 (captured 2026-09-18 > T = 2026-09-16T09:00).
- Prefer line-level timestamps: when a unit (trace group / line / record) carries its own timestamp `<= T`, use it.
- Every link that came from a post-T file is logged with `via=identifier_post_audit` in the decision log.
- Flag `--no-post-audit-links` disables D12. Acceptance: report sample recall and false positives with and without it.

## 3. Output contract

- Header exactly `case_id,deletion_status,steps_json`; 518 rows; **row order == input file order** (numeric case_id, D7).
- `deletion_status ∈ {COMPLETE, INCOMPLETE, NOT_APPLICABLE}`; `NOT_APPLICABLE ⇒ steps_json == []`.
- Each step has exactly the keys `step_no, phase, system_id, resource_id, operation, state, evidence_id`.
- `step_no` = 1..n consecutive. Phases ordered FINDING → REMEDIATION → VERIFICATION.
- COMPLETE ⇒ only VERIFICATION steps, each with a non-empty `evidence_id` (a `file_id`, `activity_id` or `audit_id`).
- INCOMPLETE ⇒ every FINDING has a non-empty `evidence_id`; every REMEDIATION and VERIFICATION has `evidence_id == ""`.
- Serialization: `json.dumps(steps, ensure_ascii=False, separators=(",", ":"))` with the fixed key order above;
  CSV written with Python `csv` (minimal quoting, `\n` line endings). Same inputs + same caches ⇒ byte-identical output.

### 3.1 Allowed (phase, operation, state) and their rank

| Phase | Rank | Operation | State |
|---|---|---|---|
| FINDING | 1 | REPORT_REMAINING_RECORD | PRESENT |
| FINDING | 2 | REPORT_EXPOSED_PII | PRESENT |
| FINDING | 3 | REPORT_ACTIVE_DATA_FLOW | ACTIVE |
| FINDING | 4 | REPORT_RESTORE_RISK | ACTIVE |
| REMEDIATION | 1 | DISABLE_DATA_FLOW | DISABLED |
| REMEDIATION | 2 | BLOCK_RESTORE | BLOCKED |
| REMEDIATION | 3 | DELETE_RECORD | ABSENT |
| REMEDIATION | 4 | REDACT_RECORD | REDACTED |
| REMEDIATION | 5 | ANONYMIZE_RECORD | ANONYMIZED |
| VERIFICATION | 1 | VERIFY_RECORD_ABSENT | ABSENT |
| VERIFICATION | 2 | VERIFY_RECORD_PRESENT | PRESENT |
| VERIFICATION | 3 | VERIFY_RECORD_REDACTED | REDACTED |
| VERIFICATION | 4 | VERIFY_RECORD_ANONYMIZED | ANONYMIZED |
| VERIFICATION | 5 | VERIFY_DATA_FLOW_DISABLED | DISABLED |
| VERIFICATION | 6 | VERIFY_RESTORE_BLOCKED | BLOCKED |

Ordering (D6): sort key `(phase_index, rank, system_id, resource_id)`, plain string comparison. Verified against
sample case_11: `account_service/account_4248 < search_service/search_052`, and `analytics_platform < support_platform`.

## 4. Pipeline stages and interfaces

```
S1 load ─► S2 intent ─► S3 identity ─► S4 extract ─► S5 link ─► S6 state@T ─► S7 policy ─► S8 steps ─► S9 serialize ─► S10 validate
```

| Stage | Input | Output | Deterministic? |
|---|---|---|---|
| S1 load | `dataset/*.csv`, manifest | typed tables | yes |
| S2 intent | `request_text` | `{authorizes_deletion: bool, rationale, source: llm\|cache\|fallback}` | LLM (D1), cached |
| S3 identity | request email, identities, audit log ≤ T | `Subject{user_id, emails[], phones[], hashes[], names[]}` | yes |
| S4 extract | evidence files | `Observation` records (§5) | yes, except PNG (D2) |
| S5 link | Subject, Observations, activity | `Link{resource_id, system_id, via, source_id}` | yes; timing module behind a flag (D3) |
| S6 state@T | linked resources | `ResourceState` per resource (§6) | yes |
| S7 policy | ResourceState, `deletion_rules.csv` | `Decision{category, category_source, required_action, compliant: bool}` | yes |
| S8 steps | Decisions | findings / remediation / verification lists | yes |
| S9 serialize | steps | CSV row | yes |
| S10 validate | `output.csv` | pass/fail + report | yes |

### S2 intent (D1)
- One LLM call per **unique** `request_text` (27 in eval), result cached on disk keyed by `sha256(text)`.
- `temperature=0` (supported on the Haiku model, see §9). Structured JSON output.
- Keyword fallback is used **only** if the API call fails. The fallback and its trigger are logged.
- Must handle: 6 languages, negations ("not asking for deletion", "don't delete or change anything"), partial
  scopes ("remove me from marketing lists only"), corrections, access/export requests.

### S3 identity
- Request email is matched case-insensitively against current `user_identities.email` and against every
  `user_audit_log.old_value` where `field_name='email'`. 418 eval emails match a current identity and 100 match only a historical one.
- Subject identifiers = current row ∪ all audit-log old/new values (email, phone, email_hash) with `event_time <= T`, plus `user_id` and `name`.
- Matching uses token boundaries (`support_864` must not match `support_8642`, `user_1` must not match `user_13`).
  Emails match case-insensitively (20 eval emails are uppercase). Near-miss emails are **not** matches
  (`g.silva76.jr@gmail.com` is not `g.silva76@gmail.com`).

### S4 extraction: parsers by format (94 files)
| Format | Count | Unit of association |
|---|---|---|
| `.log` key=value / JSON-lines / RFC5424 syslog / cloud-runtime nested `message=` / `record_lookup` lines | 26 | **trace group** (`trace_id`, `requestId`, `tr-…` in syslog); `record_lookup` and `seq=` lines are single units |
| `.json` snapshot bundles, service_resource_export, connector config, export jobs, analytics report, ledger | 28 | the JSON object that carries `resource_id` |
| `.sql` inventory (typed tables, `snapshot_scope`) and primary/replica pairs (`record_json` column) | 24 | one row; loaded with `sqlite3` in memory |
| `.txt` handover (`ts \| system \| resource \| category \| result \| subject`) | 1 | one line |
| `.pdf` | 4 | whole document (text layer via `pypdf`, no LLM) |
| `.png` | 11 | whole screenshot via vision extraction (D2) |

### S4 PNG extraction (D2)
- The vision prompt lives in `code/prompts/vision_extract.md`. The output is cached as JSON per `file_id` and reused on every run.
- Every extracted id must match the expected pattern (`^(account|search|support|analytics|audit_record|data_flow|backup|export|tombstone|marketing|object|cache|email_delivery|legal_hold)_\d+$`,
  `^user_\d+$`, email RFC-lite). A non-matching value is stored as `unparsed` and flagged, never guessed.
- Cross-check: each of the 11 PNGs (and the account-history PDF) has a `LINK_RESOURCE_IDENTITY` activity
  timestamped exactly `captured_at + 7 min` (activity_4000–4011). Per D11 the activity only **corroborates**. The
  screenshot content is the observation. When they disagree, the resource is **unlinked and flagged** (D13), never
  attributed from the activity.
- D13 (resolved): `marketing_profile_console.png` shows *Soo-jin Park / sj.park@naver.com / +82 10-5837-9451 /
  marketing_001*. A search of `user_audit_log.csv` old/new values and `user_identities.csv` (email, name, digit-normalized
  phone) found **no match** (2026-10-24), so marketing_001 stays unlinked. `activity_4008` → user_09 is a claim only.

## 5. Observation schema (output of S4)

```
Observation{
  source_id      : file_id | activity_id | audit_id
  observed_at    : captured_at (file) or event_time (activity/audit)
  system_id      : as named in the record (D9); fallback prefix map §7.1
  resource_id    : str
  category       : data_category | None      # from record_type / collection / table when explicit
  identifiers    : {emails, phones, hashes, names, user_refs} present in the record
  state_fields   : raw fields e.g. rows, http_status, result, enabled, restore_allowed, account_status
  locator        : path:line or table:row, for the decision log
}
```

## 6. State reconstruction at T (S6)

The state for a resource is taken from the **latest** usable file Observation (`captured_at <= T`) that observes it.
Activity `reported_result` values are claims and **never set state** (D11). A linked resource with no file observation
`<= T` gets no step, and this is logged as `no_state_observation`. Activity rows may be recorded in the decision log as
corroboration (e.g. `CHECK_DATA_FLOW … remains enabled` agreeing with a connector config observation).

| Observed | Normalized state |
|---|---|
| `rows=0`, `http_status=404`, `NOT_FOUND`, `profile: null` with 404 | ABSENT |
| row present with a subject identifier; `FOUND`; `IDENTIFIABLE`; `result=hit` | PRESENT_IDENTIFIABLE |
| row present, all direct identifiers null, `ANONYMIZED` | ANONYMIZED |
| identifiers removed but record kept (`REDACTED`; tombstone with name/email/phone/hash null) | REDACTED |
| connector `enabled=true` / `DATA_FLOW ACTIVE` / CHECK_DATA_FLOW "remains enabled" | FLOW_ACTIVE |
| connector `enabled=false` / `DISABLED` | FLOW_DISABLED |
| backup `restore_allowed=1` / `ACTIVE` / CHECK_RESTORE_ACCESS "can still be restored" | RESTORE_ALLOWED |
| backup `restore_allowed=0` / `BLOCKED` | RESTORE_BLOCKED |
| audit record present, `retention_until > T` | RETAINED |

**Absence by omission (Round 1, rule (c), `--absence-rule fallback_delete`, default).** A snapshot that lists a collection and
omits the resource proves ABSENT **only if** (1) there is no explicit observation of the resource `<= audit_at`, and (2) the
resource's policy is DELETE. Omission from a partial snapshot can show deletion, never that a retained or redacted record is gone.
Rejected alternatives, measured on the tuning split, are in evaluation_report.md §3.1. Explicit observations come first, so
the evidence cited for absence is the latest explicit 404 / NOT_FOUND / rows=0 file, and a snapshot only when none exists
(rule R4, 8/8 fit). Each copy is its own **store**: a replica table is not cleared by a primary-store snapshot.

**Replica drift** (verified, must not be missed): 13 rows across 11 SQL files exist **only in a replica**
(`*_replica*` table) and not in `*_primary`. Each maps to exactly one case. Examples: evidence_54 → account_005 (case_05),
evidence_05 → account_994 (case_16), evidence_15 → account_8023 (case_153, historical email). The replica row wins: the
resource is PRESENT_IDENTIFIABLE in that system and a REPORT_REMAINING_RECORD finding cites that SQL file. **All 11**
eval drift cases (case_105, 153, 211, 259, 261, 307, 355, 357, 405, 453, 511) have `audit_at == captured_at`, so the
inclusive boundary is load-bearing.

## 7. Policy (S7) and step generation (S8)

### 7.1 Category resolution (D5): first rule that fires wins, and the source is logged
1. Explicit category in the record (`record_type`, handover category column, `collection`/table name).
2. The deletion_worker's attempted operation on that resource (DELETE_RECORD ⇒ a DELETE category, ANONYMIZE_RECORD ⇒ ANONYMIZE, etc.),
   used only as a hint (sample case_12: `analytics_9914`, a contact record keyed by the *old* email hash, is DELETEd).
3. System default from the resource prefix:

| prefix | system_id (fallback) | default category |
|---|---|---|
| account | account_service | USER_PROFILE |
| search | search_service | SEARCH_INDEX_ENTRY |
| support | support_platform | SUPPORT_MESSAGE (SUPPORT_ATTACHMENT when the record is an attachment) |
| analytics | analytics_platform | ANALYTICS_PROFILE |
| audit_record | security_service | SECURITY_AUDIT_LOG |
| data_flow | integration_service | (data flow) |
| backup | backup_service | BACKUP_ARCHIVE |
| export | export_service | USER_EXPORT |
| tombstone | identity_service | ACCOUNT_TOMBSTONE |
| cache | cache_service | CACHE_ENTRY |
| email_delivery | email_delivery_service | EMAIL_DELIVERY_LOG |
| marketing | marketing_platform | MARKETING_PROFILE |
| legal_hold | legal_service | LEGAL_HOLD_RECORD |

`system_id` is the system named in the evidence record (D9). It is **not** the activity `target_system` (e.g. audit records target
`archive_service` but are reported under `security_service`; exports target `object_store` but are reported under `export_service`).

### 7.2 Decision → steps

| Required action | State at T | FINDING | REMEDIATION | VERIFICATION |
|---|---|---|---|---|
| DELETE | PRESENT_* | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| DELETE | ABSENT | — | — | VERIFY_RECORD_ABSENT |
| REDACT | PRESENT_IDENTIFIABLE | REPORT_EXPOSED_PII | REDACT_RECORD | VERIFY_RECORD_REDACTED |
| REDACT | REDACTED | — | — | VERIFY_RECORD_REDACTED |
| ANONYMIZE | PRESENT_IDENTIFIABLE | REPORT_EXPOSED_PII | ANONYMIZE_RECORD | VERIFY_RECORD_ANONYMIZED |
| ANONYMIZE | ANONYMIZED | — | — | VERIFY_RECORD_ANONYMIZED |
| RETAIN (audit / legal hold) | RETAINED | — | — | VERIFY_RECORD_PRESENT |
| data flow | FLOW_ACTIVE | REPORT_ACTIVE_DATA_FLOW | DISABLE_DATA_FLOW | VERIFY_DATA_FLOW_DISABLED |
| data flow | FLOW_DISABLED | — | — | VERIFY_DATA_FLOW_DISABLED |
| BACKUP_ARCHIVE (RETAIN + block) | RESTORE_ALLOWED | REPORT_RESTORE_RISK | BLOCK_RESTORE | VERIFY_RESTORE_BLOCKED |
| BACKUP_ARCHIVE | RESTORE_BLOCKED | — | — | VERIFY_RESTORE_BLOCKED |

- `deletion_status = INCOMPLETE` iff any FINDING exists, otherwise `COMPLETE`.
- **Verification covers every relevant resource**, including ones already compliant (sample case_11 steps 9 and 11, and case_06).
- **Fix 2 (Round 1).** In an INCOMPLETE case, a resource linked through an evidence file but with no state observation
  `<= audit_at` gets the verification of its required final state (empty evidence). Activity-only links get no step (D11, D14).
- ACCOUNT_TOMBSTONE: the stable `user_id` reference may remain; name, email, phone and email_hash must be null.
- The DELETE vs ANONYMIZE choice for an analytics record that is "no longer attributable" follows §7.1.

### 7.3 Evidence citation (D4)
- (R4, 8/8 on tuning gold) For absence, the latest explicit absence observation wins over any later snapshot omission.
- Findings and COMPLETE verifications cite the **latest evidence file** (`captured_at <= T`) that observes the
  resource in the state being claimed (absence-by-coverage counts as observing). Activity ids are not cited for
  state (D11). Consistent with the gold: of 193 sample steps, all 75 non-empty citations are `evidence_*` file ids
  (the other 118 are empty future steps). No gold step cites an activity_id or audit_id.
- Ties at the same `captured_at` are broken by the lowest `file_id`.
- Acceptance: the evidence_id match rate on the 32 samples is reported. Every mismatch is investigated and explained in
  `evaluation_report.md`, never patched case by case.

## 8. Linking (S5)

| `via` | Rule | Default |
|---|---|---|
| `identifier` | a subject identifier co-occurs with the resource_id inside one unit (§4 S4 table) of a file with `captured_at <= T` | on |
| `identifier_post_audit` | same, in a file captured after T (D12); ownership only, never state or citation | on (`--no-post-audit-links` disables) |
| `catalog` | `LINK_RESOURCE_IDENTITY` activity naming a subject identifier (proposal, D11 revised) | on |
| `timing` (D3, D15) | deletion_worker events in `[requested_at, reported_at]` whose result matches `reported_status`; global one-to-one assignment across all requests (eval + sample request rows, never their answers); link **only when exactly one candidate remains**; ambiguous candidates are logged and left unlinked | flag `--timing-links`; default decided by data (§15) |

`catalog` (D11 revised): a `LINK_RESOURCE_IDENTITY` activity naming a subject identifier proposes a link. The proposal
is rejected when the resource's observations point only to other users (the observation wins), and is logged.
A linked resource without any file observation `<= audit_at` gets no state. `object_19` and `marketing_001` therefore
produce no step (D13, D14).

D3 investigation result (2026-10-24): **no explicit join exists** for `analytics_9406` (case_03) or
`account_6440` (case_21). Each id occurs in exactly three places: one activity row (`activity_3852`, `activity_3661`),
`sample_output.csv`, and one evidence observation with all identifiers null (`evidence_94` lines 42–43 with its own
trace `rb-095409-021`; `evidence_92` with `"profile": null, "status_code": 404`). There are no batch/job/run keys on worker
events. The only `batch=` keys (`rp-7718`) belong to an unrelated support-replica log. Window + result alone
leaves 1 candidate in only 5 of the 32 samples. A loose identifier linker leaves 36 of 152 sample step resources unlinked.
The A-vs-B comparison is reported in the evaluation report.

## 9. LLM usage (D1, D2, D10 revised)

Provider: **Google Gemini** (D10 revised). Client: `code/llm/__init__.py`, one interface `LLMClient.generate(task, prompt,
images=, json_schema=)`. Provider, per-task model IDs and list prices live in `code/config.toml`, never in logic. Responses
are cached on disk (key = provider + model + task + inputs), and every call is appended to `code/cache/llm_calls.jsonl` with
token usage. `temperature` is sent and, if a model rejects it, retried once without it and memoized for that model.

Models API listing (`.venv/bin/python code/tools/list_models.py`, 2026-10-24): 46 generateContent models. Image-input
support is not a field of the Gemini models API, so it is taken from the pricing page.

| Use | Model in `config.toml` | List price in / out per 1M tokens | Status |
|---|---|---|---|
| intent (S2) | `gemini-2.5-flash`, **batched** (80 texts per call, 5 calls for 398 unique texts) | $0.30 / $2.50 | **D18 approved** |
| screenshots (S4) | `gemini-3.5-flash-lite` (won the D17 comparison) | $0.30 / $2.50 | chosen by data (evaluation_report.md §4.2) |

D17 approved `gemini-3.8-flash` for intent; it timed out on every request on 2026-10-24. `gemini-3.6-flash` then hit the
free-tier quota (20 requests/day/model). On the free tier a run is feasible only with batching and the cache.

Price source: https://ai.google.dev/gemini-api/docs/pricing (page "last updated 2026-10-22"). `usage_report.md` reports cost at
these list prices even on the free tier. Until approval: intent uses the keyword fallback (labelled TEMPORARY in code and
run log), and PNGs are a stub that logs "vision pending".

## 10. Decision log

For every case, `code/cache/decisions/<case_id>.json` records the subject identifiers, every linked resource with `via` and
locator, the state source, `category_source`, the chosen evidence and rejected alternatives, and any flags (ambiguous link,
vision mismatch, post-T file skipped). This feeds the evaluation report and the AI interview.

## 11. Tests and invariants (S10, run on every build)

1. 518 rows, header exact, order == input order, unique case_ids.
2. Every `steps_json` parses, step_no is consecutive, keys are exact, and (phase, operation, state) is in §3.1.
3. Phase order and the §3 sort key hold within each case.
4. NOT_APPLICABLE ⇔ `[]`. COMPLETE ⇒ VERIFICATION only, non-empty evidence. INCOMPLETE ⇒ evidence rules of §3.
5. Every cited evidence_id exists in the manifest / activity / audit log and has `observed_at <= audit_at`.
6. There is no remediation without a matching finding, and every remediated resource has a matching verification.
7. Regression: the 32 samples are scored by `code/evaluation/main.py` (metrics in §12).
8. `COMPLETE`/`INCOMPLETE` with an empty array is reported as a **warning** (`empty_steps`), not a failure: the problem
   statement does not forbid it, but an unproven COMPLETE is almost certainly wrong. The scorer still counts it as wrong.
Unit tests: `python3 -m unittest discover -s code/tests` (token boundaries incl. support_864 vs support_8642, identifier
normalization, historical-email resolution, all six log dialects, ordering, intent fallback).

## 12. Evaluation (`code/evaluation/main.py`; written up in `code/evaluation/evaluation_report.md`, D8)

`python3 code/evaluation/main.py --pred <csv> [--gold dataset/sample_output.csv] [--json-out r.json]`

- **Case status accuracy**: share of gold cases with the exact `deletion_status`, with a per-gold-status breakdown and a confusion list.
- **Step-level**: a step = `(phase, system_id, resource_id, operation, state, evidence_id)`. Per-case multiset match gives
  P / R / F1 and accuracy = matches / Σ max(|gold|, |pred|). Also reported on the identity key `(phase, operation, system_id,
  resource_id)`, which separates discovery errors from state and citation errors.
- **Field-level**: gold and predicted steps are aligned with difflib on the identity key. Accuracy per field (`system_id, resource_id,
  operation, state, evidence_id`, plus `phase`) = matching pairs / (pairs + unpaired gold + unpaired pred).
- **Sequence accuracy**: share of cases whose full array (all keys including `step_no`, in order) equals gold.
- **Per-case diffs**: MISSING / EXTRA steps, WRONG fields (same identity key, different state/evidence), ORDER, STEP_NO, parse errors.
- Runs to report: default; `--timing-links` (D3 A vs B); `--no-post-audit-links` (D12 with vs without).
- Custom cases (§13) are listed with their inputs, expected output and result.

Verified 2026-10-24: gold vs gold = 100% on every metric. An all-`NOT_APPLICABLE`/`[]` prediction = 0% on every metric, no crash.
`validate.py` passes on `sample_output.csv` (so the gold obeys §3 ordering and the evidence-time rule) and fails the blank template.

## 13. Custom evaluation cases (proposed; the samples have 0 NOT_APPLICABLE)

These cases reuse real dataset identities and evidence. Only the request row is synthetic, and the expected output is hand-derived and listed in the report.

| ID | Request (synthetic row) | Expected | What it proves |
|---|---|---|---|
| E1 | `kwame-mensah@proton.me`, text "Please remove me from marketing lists only. Keep my account and orders." | `NOT_APPLICABLE, []` despite live marketing/account evidence for user_09 | deletion keyword + partial scope ≠ deletion |
| E2 | `amara-okeke@proton.me`, Spanish: "No quiero eliminar mi cuenta; solo necesito una copia de mis datos." | `NOT_APPLICABLE, []` | negation in a non-English language |
| E3 | `minjun.kim90@gmail.com` (historical email of user_12), Japanese correction request "電話番号を更新してください。アカウントは削除しないでください。" | `NOT_APPLICABLE, []`, and the subject must still resolve to user_12 in the decision log | historical identity + non-deletion |
| E4 | clone of case_16 (`diya.k4436@outlook.com`) with `audit_at = 2026-09-21T09:59:59Z` (1 s before evidence_05) | `account_994` is **not** reported from evidence_05; state comes from earlier sources | inclusive cutoff and no post-T evidence |
| E5 | `g.silva76@gmail.com`, deletion request, `audit_at = 2026-09-02T08:00:00Z` | `search_428` (user_13, evidence_09) is reported; `search_1874` (`g.silva76.jr@gmail.com`) appears in no step | near-miss decoy rejection + screenshot extraction |

## 14. Decision record for former open items

| Item | Decision | Status |
|---|---|---|
| N1 post-T linking | D12-A: ownership only, logged, flag to disable | resolved |
| N2 model IDs | D10 revised, D17: Gemini; see §9 | intent: substitute pending decision; vision: gemini-3.5-flash-lite by comparison |
| N3 marketing_001 | D13-B: unlinked and flagged; identity search found no match | resolved |
| N4 object_19 | D14-A: excluded (activity-only link, D11) | resolved |

Original wording of the items, kept for traceability:

- **N1 — linking from post-T files.** Example: sample case_09 (INCOMPLETE, audit 2026-09-16T09:00) has gold steps for
  `account_3960` and `audit_record_4902`. Their only identifier link is in evidence_06, whose **lines** are timestamped 09-16T08:52–08:57
  (≤ T) but whose **file** was captured 2026-09-18 (> T).
  A: allow post-T files for identity linking only (never cited, never state). B: strict file-level cutoff.
- **N2 — model IDs.** Confirm `claude-haiku-4-5` + `claude-sonnet-5` in place of the IDs in D10.
- **N3 — screenshot vs catalog conflict.** `marketing_profile_console.png` (evidence_60) shows *Soo-jin Park, sj.park@naver.com,
  marketing_001*. No such user exists in `user_identities.csv`, yet `activity_4008` links `marketing_001` to
  `kwame-mensah@proton.me` / user_09, who has an eval case (case_63). The other 10 screenshots agree with their catalog rows.
  A: catalog link wins (attribute marketing_001 to user_09 and cite the screenshot/activity). B: treat the conflicting record as unlinked and flag it.
- **N4 — `object_19`.** `activity_4007` says delivery_confirmation_19.pdf "belongs only to priya.raman@gmail.com and user_07",
  but sample case_07 gold contains only `support_001`, not `object_19`. No other source mentions object_19 after 2026-02-13.
  A: exclude the `object_*` system from findings unless a state observation exists ≤ T (matches the gold). B: report it as a remaining SUPPORT_ATTACHMENT.

## 15. Baseline 2026-10-24 (32 samples, no tuning)

| Metric | `--timing-links` off | on |
|---|---|---|
| status accuracy | 93.75% | 93.75% |
| step precision / recall / F1 | 97.95 / 74.09 / 84.37% | 94.84 / 76.17 / 84.48% |
| evidence_id accuracy (field) | 74.23% | 75.13% |
| sequence accuracy | 15.62% | 15.62% |

Missing gold steps by root cause, timing off (50 missing of 193; 49 by identity key):
- 23: **identifier-free resources.** 404 account lookups without an email and anonymized analytics, linkable only through activity (13 activity-only plus 10 observed without identifiers).
- 17: **state.** A later *partial* snapshot (evidence_51 at exactly `audit_at`, the empty inventory evidence_90) overrode an
  explicit observation as "absent by coverage" (cases 05, 17, 21, 24, 25, 27, 28).
- 6: **intent.** The temporary keyword fallback missed case_26 ("confirm that the profile is gone").
- 3: **unreachable.** `audit_record_5569`, `audit_record_6645` and the INCOMPLETE-only verification of post-audit-linked `audit_record_4128` have no `<= audit_at` observation (two ids occur in no participant file).

Extra steps: 2 with timing off (one stale March lookup `account_6523`, one catalog proposal `tombstone_17`); 5 with timing on (+3 wrong
timing assignments). No parser gaps, identity misses or ordering errors among the top-10 diffs. **No fix has been applied yet** (user instruction).

## 16. Round 1 decisions (2026-10-24)
- **D16 holdout:** case_04, 08, 12, 16, 20, 24, 28, 32 (every 4th, `code/evaluation/splits.py`). Aggregates only; contamination
  before the freeze is disclosed in evaluation_report.md §1.1.
- **D17:** LLM intent wired in (batched, cached); vision comparison done; `gemini-3.5-flash-lite` chosen.
- **State:** absence rule (c) plus fix 2 restricted to evidence-linked resources. 12 → 0 state-caused misses on the tuning split.
- **Citation:** R4 (explicit absence observation first) fits 8/8 tuning absence citations.
- **Irreducible:** `audit_record_5569` and `audit_record_6645` occur in no participant file.

## 17. Decisions D18, D19 and standing rule (2026-10-24)
- **D18 (intent model, replaces D17 for intent):** batched `gemini-2.5-flash`. Billing is not worth it for a cached step.
  Condition: each batch response must return exactly the input ids (no missing, extra or duplicate ids), or the whole batch
  falls back to the keyword rules. Every text where the LLM and the keyword rules disagree is listed in the run log
  (`intent disagreement ...`) for manual review.
- **D19:** absence rule (c) is the default (`--absence-rule fallback_delete`). Omission can prove a record was deleted, never
  that a retained record is gone. Fix 2 is restricted to evidence-linked resources, consistent with D14.
- **Standing rule:** at the end of every round, commit and push to the working branch without asking.
