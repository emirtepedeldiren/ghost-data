# Evaluation report: Ghost Data Still Exists audit agent

Status: **in progress (after Round 2, 2026-10-24)**. Sections marked TODO are filled in later rounds.

## 1. How we measure

- Gold: `dataset/sample_output.csv` (32 cases). The participant data contains no other answers.
- Scorer: `python3 code/evaluation/main.py --pred <csv> --split {all,tuning,holdout}`.
  - **Status accuracy**: share of cases with the exact `deletion_status`.
  - **Step-level**: a step is `(phase, system_id, resource_id, operation, state, evidence_id)`. Per-case multiset match gives
    precision, recall and F1.
  - **Field-level**: gold and predicted steps are aligned on `(phase, operation, system_id, resource_id)`; accuracy is computed per field.
    This report quotes `evidence_id`.
  - **Sequence accuracy**: share of cases whose full `steps_json` array equals gold exactly (order and step_no included).
- Validator: `python3 code/validate.py` (structure, ordering, evidence rules, evidence captured `<= audit_at`).
- Root-cause attribution: `python3 code/evaluation/rootcause.py` (tuning split only).

### 1.1 Holdout (D16)
The holdout is every 4th sample case in numeric order: `case_04, case_08, case_12, case_16, case_20, case_24, case_28,
case_32` (`code/evaluation/splits.py`). From Round 1 on, we tune only on the other 24 cases, and the tools never print
per-case diffs for holdout cases.

**Contamination disclosure.** Everything before the freeze looked at all 32 cases:
- the baseline diagnosis showed per-case diffs of case_04, case_20, case_24 and case_28;
- the Phase 0 data study used case_12 (it motivated the D5 worker-hint category rule) and case_16 (replica drift).

The holdout numbers are therefore somewhat optimistic. No rule in Round 1 was derived from a holdout case: the state rule
was chosen and measured on the tuning split only.

## 2. Results

| Run | Split | Status | Step P | Step R | Step F1 | evidence_id | Sequence |
|---|---|---|---|---|---|---|---|
| Baseline (keyword intent, no vision, omission overrides) | tuning (24) | 91.67% | 97.39% | 75.17% | 84.85% | 75.33% | 16.67% |
| Baseline | holdout (8) | 100.00% | 100.00% | 70.45% | 82.67% | 70.45% | 12.50% |
| **Round 1** (LLM intent, vision, absence rule (c), fix 2) | tuning (24) | **100.00%** | **99.24%** | **87.25%** | **92.86%** | **86.67%** | **33.33%** |
| **Round 1** | holdout (8) | **100.00%** | **100.00%** | **81.82%** | **90.00%** | **81.82%** | **37.50%** |
| Round 1 + `--timing-links` | tuning (24) | 100.00% | 97.14% | 91.28% | 94.12% | 88.89% | 37.50% |
| Round 1 + `--timing-links` | holdout (8) | 100.00% | 100.00% | 81.82% | 90.00% | 81.82% | 37.50% |
| **Round 2** (L2 timing D20, D21, D22, D13 revised) | tuning (24) | **100.00%** | **99.30%** | **94.63%** | **96.91%** | **94.00%** | **62.50%** |
| **Round 2** | holdout (8) | **100.00%** | **100.00%** | **90.91%** | **95.24%** | **90.91%** | **62.50%** |

All 518 eval predictions pass the validator, with no warnings: there are no COMPLETE/INCOMPLETE rows with empty steps.
Round 2 tuning: 8 missing gold steps (the 7 symmetric ones in §3.6 plus 1 unreachable) and 1 extra step (`account_6523`).

## 3. Failures found and improvements made

### 3.1 Round 1: state reconstruction
**Failure.** A later *partial* snapshot that omitted a resource was treated as proof of absence and overrode an explicit
observation. Examples: in case_05 an empty account inventory (evidence_90, 09-05) hid a row that survived in the failover replica
(evidence_54); in several cases the bundle evidence_51, captured exactly at `audit_at`, "erased" retained audit records seen minutes earlier.

**Rules compared on the tuning split** (keyword intent, no vision, so only the rule differs):

| Absence rule | State-caused missing steps | Step P | Step R | Step F1 | Sequence |
|---|---|---|---|---|---|
| override (baseline): latest snapshot omission wins | 12 | 97.39% | 75.17% | 84.85% | 16.67% |
| (a) scope-aware: omission counts only inside the snapshot's own store/collection | 10 | 98.29% | 77.18% | 86.47% | 20.83% |
| (b) fallback: omission counts only when no explicit observation exists `<= audit_at` | 4 | 98.37% | 81.21% | 88.97% | 29.17% |
| **(c) = (b), and omission only for resources whose policy is DELETE, + fix 2** | **0** | **99.21%** | **83.89%** | **90.91%** | **33.33%** |

Why (a) is not enough: the only scope fields in the evidence are SQL `snapshot_scope(origin_system)` and the JSON bundle's
`data_store` + `collection`. A replica table is a separate store, so (a) fixes case_05. But evidence_51 declares
`security_service/security_audit_events`, the same store as the explicit observation, and lists only 2 records. It is partial
*within* its declared scope, and no field says so.

Why (c): omission from a partial snapshot can show that a record was deleted. It can never show that a record that must be
*retained* (audit log) or kept-but-redacted is gone. The 4 misses left under (b) were all retained audit records in INCOMPLETE cases.

**Fix 2.** In an INCOMPLETE case, a linked resource with no state observation still gets the verification of its required final
state (empty evidence_id). It applies only to resources linked through an evidence file. Activity-only proposals with no
observation get no step (D11, D14). The unrestricted version added `object_19` and `tombstone_17`, both wrong.

### 3.2 Citation rule for absence (case_04 question)
Tested on all 8 absence citations (`VERIFY_RECORD_ABSENT` with evidence) in the tuning gold (`code/evaluation/citation_rules.py`):

| Rule | Fit |
|---|---|
| R1 latest file `<= audit_at` showing absence | 4/8 (50%) |
| R2 earliest file after the resource's delete event | 0/8 (0%) |
| R3 same file as the case's other steps | 6/8 (75%) |
| **R4 latest explicit absence observation (404 / NOT_FOUND / rows=0); omission snapshot only if none** | **8/8 (100%)** |

R4 is exactly the evidence that absence rule (c) selects, so no separate citation logic is needed.

### 3.3 Intent
The temporary keyword fallback misclassified case_26 ("confirm that the profile is gone"; no deletion verb). With the LLM
classifier, case_26 is a deletion request (`deletion_verification`). Over all 398 unique request texts (eval + sample), exactly the 10
one-off non-deletion requests (case_533 to case_542) are NOT_APPLICABLE, and there are no keyword fallbacks.

### 3.4 Not fixing (irreducible)
`audit_record_5569` (case_20) and `audit_record_6645` (case_18) appear in **no participant file**: not in any evidence file, the activity
stream or the manifest. The gold includes them, so they are irreducible misses, and we do not chase them.

### 3.5 Open after Round 1 (tuning split; resolved in §3.6)
19 missing gold steps: 10 resources linkable only through activity rows, 8 observed only without identifiers (404 lookups, anonymized
analytics), and 1 unreachable. There is 1 extra step (`account_6523`, a stale March lookup for case_17's email). This is the linking work for Round 2.

### 3.6 Round 2: linking (D20 L2, D21, D22, D13 revised)
**Failure.** The Round 1 timing linker assigned the account events correctly as a global one-to-one, but its follow-up step
considered only requests whose account event was already assigned. When case_28's account was ambiguous, case_28 dropped out
of the pool, and `analytics_6077` looked unique to case_27. The worker also processes people who have no request at all:
`analytics_9447` runs in the same slot as `account_146` (user_31, who has no case). The result was 3 wrong analytics links.

**Signals found** (tuning gold only; holdout never inspected):
- The worker follows a schedule relative to `reported_at`: account deletes at -75..+8 min; "policy" follow-ups at about -28.5
  (analytics, tombstone), -21 (support), -14.5 (data flow) and -7 (backup) min. Seconds are random per event.
- Every tuning gold case has exactly 1 account and at most 1 resource of each type (24/24).
- Worker result ↔ reported_status holds for account events only (24/24 with PARTIALLY_COMPLETE→PARTIAL|SUCCESS).
- Rejected as signals: the order of follow-ups within a slot (support and backup run in the opposite order to data flow),
  per-case jitter, and analytics content (the metric values also occur on other users' records).

**Rule L2** is specified in CONTRACT.md §18. Ablations on the tuning split (scratchpad simulation; link counts include links
also made by identifiers):

| Variant | Tuning links correct / wrong |
|---|---|
| L2 | 29 / 0 |
| request window `[requested_at, reported_at]` instead of the schedule window | 22 / 1 |
| without the one-per-type slot rule | 24 / 1 (case_11 `analytics_9447` returns) |
| without the result filter | 24 / 0 |
| strict PARTIALLY_COMPLETE→PARTIAL | 28 / 0 |
| closed-world guard | no change (not adopted) |

**Calibration without labels** (worker events on resources linked by identifiers in the 518 eval cases, offsets from `reported_at` in minutes):

| Event type | n | min | p1 | p99 | max |
|---|---|---|---|---|---|
| account DELETE | 236 | -41232.3 | -74.8 | -44.0 | 272936.8 |
| REDACT_RECORD (policy) | 37 | -28.8 | -28.8 | -14.9 | -14.9 |
| RETAIN_RECORD (policy) | 8 | -29.0 | -29.0 | -28.1 | -28.1 |
| DISABLE_DATA_FLOW (policy) | 33 | -15.0 | -15.0 | -7.4 | -7.4 |
| BLOCK_RESTORE (policy) | 33 | -8.0 | -8.0 | -2.9 | -2.9 |
| DELETE_RECORD ("final result", outside L2 windows) | 326 | -41113.4 | 38.5 | 92.1 | 98.3 |
| REDACT_RECORD ("final result") | 126 | 38.3 | 39.0 | 113.5 | 150654.0 |
| ANONYMIZE_RECORD ("final result") | 73 | 38.2 | 38.3 | 133.0 | 82781.0 |

Two account events fall outside `[-80, +10]`, and both belong to a different request by the same subject: `account_5762`
(case_34) is a FAILED attempt four weeks before the request, and `account_6523` (case_46) is deleted six months after it.
Neither argues for a wider window, so the bounds are unchanged. There are no identifier-linked "policy" analytics events in eval
(those records are always identifier-free), so the analytics offset rests on the tuning data alone.

**Still unresolved (7 tuning steps, by design).** Same-slot, same-type resources with no identifiers are interchangeable:
`account_410` {case_23, 547, 548}, `account_7012` {28, 29}, `analytics_9406` {03, 44}, `analytics_5335` {21, 23, 546, 547, 548},
`analytics_1895`/`analytics_4566` {24, 25, 26, 549, 550}, `analytics_4574` {27, 28}. L2 leaves them unlinked.

**D21** (post-audit unit ownership, restricted). On eval, 31 such links were dropped by the restriction and 20 were also linked
another way. On top of D20 it adds **0 steps**. Alone (D20 off) it adds 2 steps, both the `account_3866` pattern (404 before the
audit, cited; a later bundle shows the profile with the subject's identifiers): case_231 `account_7324` (evidence_80) and
case_423 `account_2420` (evidence_68).

**Eval drift vs the Round 1 run** (no labels; decisions applied cumulatively, and each change is attributed to the first
decision that caused it; with every flag off the output is byte-identical to Round 1):

| Decision | Status changes | Cases with changed steps | Net steps |
|---|---|---|---|
| D20 (L2 timing) | 0 | 160 | +167 |
| D21 | 0 | 0 | 0 |
| D22 | 1: case_55 COMPLETE→INCOMPLETE | 1 | +3 |
| D13 revised | 1: case_63 COMPLETE→INCOMPLETE | 1 | +3 |

Final eval: 356 INCOMPLETE, 152 COMPLETE, 10 NOT_APPLICABLE. No COMPLETE row has empty steps.

### 3.7 Round 3 audit: policy conditions on eval-only data (no fixes yet)
Tool: `python3 code/evaluation/policy_coverage.py` → `code/evaluation/policy_coverage.md` (coverage table, 2 seeded examples
per category and branch with the raw evidence line and steps, and the full list of undeterminable conditions). No labels are used.

**Finding 1: the pipeline never reads the `condition` column.** `reconstruct.py:38` maps category → action. Each
condition is either assumed met, or decided implicitly by a state rule:

| Rule | Condition | How the code treats it | Code path |
|---|---|---|---|
| rule_05 SUPPORT_MESSAGE | shared context must be retained | assumed met → REDACT; the support message/attachment split comes from the category, not this condition | `reconstruct.py:59-66`, `vocab.py:52,73-74`, `common.py:85-90` |
| rule_06 SUPPORT_ATTACHMENT | belongs only to the subject | assumed met → DELETE; co-ownership is never checked | `vocab.py:75`, `logs.py:88`, `pdf_text.py:42` |
| rule_07 ANALYTICS_PROFILE | still attributable | **evaluated implicitly**: identifiers present → PRESENT (anonymize); all null → ANONYMIZED (verify only) | `common.py:81-82`, `logs.py:81-83`, `vision.py:100-102` |
| rule_11 EMAIL_DELIVERY_LOG | log contains an email | never reached (0 resources); the default state rule would not check for an email | `common.py:91` |
| rule_12 SECURITY_AUDIT_LOG | within retention | **assumed met**: any present record → RETAINED; `retention_until` is never parsed (CONTRACT §6 says "retention_until > T", and the code does not do it) | `common.py:79-80`, `logs.py:89`, `vision.py:98-99`, `pdf_text.py:44`, `vocab.py:121` |
| rule_13 LEGAL_HOLD_RECORD | hold active | **assumed met**, same paths, plus `logs.py:100` | as rule_12 |
| rule_14 BACKUP_ARCHIVE | restore can be blocked | assumed met; the action is hard-coded to BLOCK_RESTORE, while restore state is evaluated from flags (plus D22) | `reconstruct.py:64-65`, `common.py:77-78`, `logs.py:93-97`, `vision.py:92-97`, `reconstruct.py:125` |
| rule_15 ACCOUNT_TOMBSTONE | account deleted, only user_id needed | assumed met → REDACT; only identifier presence is evaluated | `common.py:83-84`, `vision.py:100-102` |
| (DATA_FLOW) | no rule in deletion_rules.csv | hard-coded DISABLE_FLOW | `reconstruct.py:62-63` |
| (worker hint) | D5 rule 2 | with no explicit category, the worker's attempted operation sets the action and the rule is bypassed. 132 accounts, all DELETE = rule_01, so no output effect | `reconstruct.py:50-55` |

**Finding 2: expiry never occurs in the participant data.** Every retention/hold end date in the evidence is in 2027–2034:
audit records 2031-12-31 ×28, 2032-06-30 ×21, 2033-12-31 ×31, 2034-09-30 ×24, one PDF 2027-08-25; legal holds 2033-12-31 ×10.
The latest audit_at is in October 2026. Evaluating rule_12/13 would therefore change **0** eval outputs. The hypothesis that
an expired retention must not be retained cannot be tested or contradicted with this data.

**Finding 3: undeterminable conditions (431 resources) and what the code did** (in every case the condition is assumed met):

| Category | Undeterminable (`<= audit_at`) | With post-audit evidence too | What the code did |
|---|---|---|---|
| SECURITY_AUDIT_LOG | 323 of 422 | 323 (no file states their retention date) | VERIFY_RECORD_PRESENT |
| SUPPORT_MESSAGE | 95 of 170 | 84 | REDACT (137 exposed → REDACT_RECORD; 33 verify only) |
| ACCOUNT_TOMBSTONE | 7 of 9 | 7: in 6 cases (543, 544, 547-550) no account is linked at all (they sit in the L2 ambiguous slots), and 1 is `tombstone_17` (catalog only, no step) | REDACT |
| LEGAL_HOLD_RECORD | 3 of 8 | 2 | VERIFY_RECORD_PRESENT |
| SUPPORT_ATTACHMENT | 2 of 14 | 1 (`object_19`, no step, D14) | DELETE |
| BACKUP_ARCHIVE | 1 of 59 | 0 (`backup_9287`, the D22 case) | REPORT_RESTORE_RISK |

**Finding 4: rules that never fire on eval.** rule_02 SECONDARY_IDENTIFIER, rule_04 CACHE_ENTRY, rule_08 PRODUCT_TELEMETRY and
rule_11 EMAIL_DELIVERY_LOG have no linked resource. `cache_*` and `email_delivery_*` ids occur only in activity rows (INDEX/SEND_EMAIL
traffic), never in an evidence file. MARKETING_PROFILE fires once (case_63). USER_EXPORT does occur in the samples (case_23 `export_7364`).

## 4. Model selection

### 4.1 Intent (D1, D17)
- Approved: `gemini-3.8-flash`. On 2026-10-24 every request timed out after 45–90 s, as did the `gemini-flash-latest` alias.
- Substitute 1: `gemini-3.6-flash`. It worked, then hit the **free-tier quota of 20 requests/day/model** after 16 calls (HTTP 429, retry in about 17 h).
- Current (substitute 2, pending user decision): **`gemini-2.5-flash`, batched**. 398 unique texts in 5 calls of up to 80, deterministic
  batches (texts ordered by sha256), temperature 0, cached.

### 4.2 Screenshots (D2, D17)
Cross-check: each screenshot has a `LINK_RESOURCE_IDENTITY` activity (activity_4000–4011). It is a claim, not ground truth: a correct
reader of `marketing_profile_console.png` must disagree with activity_4008 (D13). The `tombstone_17` and `object_19` ids are not printed in
their screenshots, so "not found" is the correct reading for both.

| Model | Printed resource ids found (of 9) | Identity agrees with catalog | Records | Flagged invalid values | Input / output tokens | Est. cost |
|---|---|---|---|---|---|---|
| **gemini-3.5-flash-lite** | **9/9** | 7/11 (misses: export owner email not read; marketing = correct disagreement) | 37 | 14 | 15,965 / 8,322 | **$0.026** |
| gemini-2.5-flash | 8/9 (put `support_1034` in the user_id field, rejected by validation) | 7/11 | 67 | 20 | 6,919 / 37,125 | $0.095 |

Winner: **gemini-3.5-flash-lite** (higher id recall, less noise, a quarter of the cost). Known gap: it does not read emails in an "Owner"
column. The fix is a prompt change, left for a later round.

## 5. Additional evaluation cases (TODO, Round 3)
Planned E1–E5 (CONTRACT.md §13): three NOT_APPLICABLE variants (partial-scope keyword, negation in Spanish, historical email +
Japanese correction), the inclusive cutoff (case_16 clone 1 s before evidence_05), and the near-miss decoy (`g.silva76.jr`).

## 6. Token usage so far (development, not the final run)
| Model | Task | API calls | Input tokens | Output tokens | Est. cost (list price) |
|---|---|---|---|---|---|
| gemini-3.6-flash | intent (unbatched, abandoned) | 16 | 5,313 | 4,049 | $0.019 |
| gemini-2.5-flash | intent (5 batches) | 5 | 19,480 | 29,246 | $0.079 |
| gemini-2.5-flash | vision comparison | 11 | 6,919 | 37,125 | $0.095 |
| gemini-3.5-flash-lite | vision | 11 | 15,965 | 8,322 | $0.026 |
| **total** | | **43** | 47,677 | 78,742 | **$0.219** |

The final full-dataset run is reported in `usage_report.md`. With a warm cache it makes 0 API calls.
