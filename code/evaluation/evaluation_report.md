# Evaluation report: Ghost Data Still Exists audit agent

Final version (frozen run `frozen_v1`, 2026-10-24). All numbers come from the frozen configuration
`code/config/frozen_v1.toml` (cache-only, 0 live API calls) unless a section says otherwise.

## 0. Summary

| Split | Cases | Status accuracy | Step precision | Step recall | Step F1 | Field accuracy (each field) | Sequence accuracy |
|---|---|---|---|---|---|---|---|
| Tuning | 24 | **100.00%** | **99.30%** | **94.63%** | **96.91%** | **94.00%** | **62.50%** |
| Holdout (D16) | 8 | **100.00%** | **100.00%** | **90.91%** | **95.24%** | **90.91%** | **62.50%** |
| All samples | 32 | 100.00% | 99.45% | 93.78% | 96.53% | 93.30% | 62.50% |

- **Eval set (518 rows, no labels):** the validator passes with 0 errors and 0 warnings. Statuses: 356 INCOMPLETE, 152 COMPLETE,
  10 NOT_APPLICABLE.
- **Participant evaluation cases:** E1–E5 pass, 5/5 (§6).
- **What is left on tuning:** 8 missing gold steps. 7 are interchangeable "twin" resources that we deliberately leave unlinked (D29),
  and 1 is unreachable (its id is in no participant file). There is 1 extra step (`account_6523`, D28).
- **Holdout vs tuning:** the holdout improved at the same rate as tuning across the rounds (sequence 12.5 → 62.5%), which is the
  main evidence that the rules generalize.

## 1. How we measure

- **Gold:** `dataset/sample_output.csv` (32 cases). The participant data contains no other answers.
- **Scorer:** `python3 code/evaluation/main.py --pred <csv> --split {all,tuning,holdout}`.
  - **Case-level status accuracy:** share of cases with the exact `deletion_status`, broken down by gold status.
  - **Step-level:** a step is `(phase, system_id, resource_id, operation, state, evidence_id)`. A per-case multiset match gives
    precision, recall, F1 and accuracy = matches / Σ max(|gold|, |pred|).
  - **Field-level:** gold and predicted steps are aligned with difflib on `(phase, operation, system_id, resource_id)`. Accuracy per field
    = matching pairs / (pairs + unpaired gold + unpaired pred). In the final run every field scores the same as the step accuracy,
    because every remaining error is a whole missing or extra step. No aligned step has a wrong state or citation.
  - **Sequence accuracy:** share of cases whose full `steps_json` array (all keys, order and `step_no` included) equals gold.
- **Validator:** `python3 code/validate.py --output output.csv`. It checks structure, the phase/operation/state vocabulary, ordering,
  consecutive step numbers, evidence rules, every cited id exists and was captured `<= audit_at`, and NOT_APPLICABLE ⇔ `[]`.
- **Root-cause attribution:** `python3 code/evaluation/rootcause.py` (tuning split only).
- **Policy coverage audit:** `python3 code/evaluation/policy_coverage.py` → `policy_coverage.md` (eval only, no labels).

### 1.1 Holdout and contamination (D16)
The holdout is every 4th sample case in numeric order: `case_04, 08, 12, 16, 20, 24, 28, 32` (`code/evaluation/splits.py`), frozen
before any tuning. From Round 1 on, all rules were chosen on the 24 tuning cases, and the tools never print per-case diffs for holdout cases.

**Contamination disclosure.** Before the freeze, the Phase 0 study and the baseline diagnosis looked at all 32 cases:
- the baseline diagnosis printed per-case diffs of case_04, 20, 24 and 28;
- the data study used case_12 (it motivated the D5 worker-hint category rule) and case_16 (replica drift).

The holdout numbers are therefore somewhat optimistic. After the freeze, no rule was derived from a holdout case. One custom case
(E4) was originally specified as a case_16 clone and was moved to eval case_153 to keep the holdout clean.

## 2. Final results in detail (frozen_v1)

### 2.1 Tuning (24 cases)
| Metric | Value |
|---|---|
| Status accuracy | 100.00% (gold COMPLETE n=8: 100%; gold INCOMPLETE n=16: 100%) |
| Step-level accuracy / P / R / F1 | 94.00% / 99.30% / 94.63% / 96.91% (matched 141, gold 149, pred 142) |
| Field-level: phase, system_id, resource_id, operation, state, evidence_id | 94.00% each |
| Sequence accuracy | 62.50% (gold COMPLETE 37.50%, gold INCOMPLETE 75.00%) |

### 2.2 Holdout (8 cases, aggregate only)
| Metric | Value |
|---|---|
| Status accuracy | 100.00% (gold COMPLETE n=2, gold INCOMPLETE n=6) |
| Step-level accuracy / P / R / F1 | 90.91% / 100.00% / 90.91% / 95.24% (matched 40, gold 44, pred 40) |
| Field-level, each field | 90.91% |
| Sequence accuracy | 62.50% |

### 2.3 Eval set (518 cases, no labels)
- Validator: PASS, 0 errors, 0 warnings. No row hits D27 `NO_OBSERVABLE_RECORDS`.
- Status distribution: 356 INCOMPLETE / 152 COMPLETE / 10 NOT_APPLICABLE. The 10 NOT_APPLICABLE are exactly the one-off non-deletion
  requests (case_533–542: access, export, correction, marketing opt-out, "do not delete").
- Identity: 418 request emails match a current identity, 100 only a historical one.
- Output: `output.csv` sha256 `774aa9ca3e0073383fc9251ce17217b684c4b06161c5385c712945a420d1cb1f`, produced by config sha256
  `23f3756663ed21faee986f397344a1c43e7479dfe540764dc570fba87c1e6aa9`.

## 3. Results round by round (before → after)

| Round | Main change | Tuning status | Tuning P / R / F1 | Tuning evidence_id | Tuning sequence | Holdout P / R | Holdout sequence |
|---|---|---|---|---|---|---|---|
| Baseline | deterministic core, keyword intent, no screenshots | 91.67% | 97.39 / 75.17 / 84.85 | 75.33% | 16.67% | 100 / 70.45 | 12.50% |
| Round 1 | state rule (c), fix 2, citation R4, LLM intent, screenshots | 100.00% | 99.24 / 87.25 / 92.86 | 86.67% | 33.33% | 100 / 81.82 | 37.50% |
| Round 2 | linking rule L2 (D20), D21, D22, D13 revised | 100.00% | 99.30 / 94.63 / 96.91 | 94.00% | 62.50% | 100 / 90.91 | 62.50% |
| Round 3 | conditions evaluated (D23), audit, E1–E5 | unchanged (output byte-identical) | | | | | |
| Phase 4 | freeze, cache-only, D27 explicit branch | unchanged (output byte-identical) | | | | | |

Missing tuning gold steps: 50 (baseline, all 32 cases) → 19 (Round 1, tuning) → 8 (Round 2, tuning).

## 4. Failures found and improvements made

### 4.1 Baseline diagnosis (Phase 0 → baseline)
On the 32 samples, 50 gold steps were missing:
- 23 identifier-free resources (404 lookups, anonymized analytics), linkable only through activity;
- 17 state errors (a later partial snapshot overrode an explicit observation);
- 6 from intent (the keyword rules missed case_26, "confirm that the profile is gone");
- 3 unreachable.

Precision was already 97.95%: the system rarely invented steps, it missed them.

### 4.2 Round 1: state reconstruction
**Failure.** A later *partial* snapshot that omitted a resource was treated as proof of absence and overrode an explicit
observation. Examples: in case_05 an empty account inventory (evidence_90) hid a row that survived in the failover replica (evidence_54);
the bundle evidence_51, captured exactly at `audit_at`, "erased" retained audit records seen minutes earlier.
**Fix:** absence rule (c) plus fix 2 (§5.2). State-caused misses on tuning went from 12 to 0.

**Fix 2.** In an INCOMPLETE case, a resource linked through an evidence file but with no state observation still gets the verification
of its required final state (empty evidence_id). Activity-only proposals get no step (D11, D14). The unrestricted version wrongly added
`object_19` and `tombstone_17`.

### 4.3 Round 1: absence citation
Gold cites the latest **explicit** absence observation, not the latest snapshot (§5.3, rule R4, 8/8). No separate citation logic was needed.

### 4.4 Round 1: intent
The temporary keyword rules misread case_26. The LLM classifier (batched, cached) fixes it and finds exactly the 10 non-deletion
requests. Each batch must return exactly its input ids (D18), and every text where the LLM and the keyword rules disagree is logged.
There is exactly 1 such text: case_26, where the LLM is right.

### 4.5 Round 2: linking
**Failure.** `--timing-links` (Round 1) reached recall 91.28% but made 3 wrong assignments, for example `analytics_6077` linked to
case_27. The account step was already a global one-to-one assignment. The follow-up step, however, considered only requests whose
account event had been assigned, so a request with an ambiguous account dropped out of the pool. The worker also processes people
who have no request at all: `analytics_9447` runs in the same slot as `account_146` (user_31, who has no case).

**Signals found** (tuning gold only):
- The worker follows a fixed schedule relative to `reported_at`: account deletes at −75..+8 min; follow-ups at about −28.5 (analytics,
  tombstone), −21 (support), −14.5 (data flow) and −7 (backup) min.
- Every tuning case has exactly 1 account and at most 1 resource of each type (24/24).
- Worker result ↔ reported_status holds for account events only.

**Fix:** rule L2 (D20, CONTRACT §18): schedule windows, a result filter for accounts, a one-resource-per-type slot rule, the intent
pool, and global elimination that links only unique candidates. Result: 0 wrong links (tuning and holdout), 11 of 18 misses fixed,
all 3 wrong assignments removed. The window bounds were calibrated without labels on eval (§5.1).

**Further fixes this round:**
- **D21:** ownership from post-audit units; a step requires a state observation `<= audit_at`.
- **D22:** a backup holding the subject's data with no observed block is a restore risk (case_55).
- **D13 revised:** a catalog link is vetoed only by an identity that resolves to *another known user* (case_63, flagged identity-mismatch).

Eval drift vs Round 1 (no labels, decisions applied cumulatively, each change attributed to the first decision that caused it):

| Decision | Status changes | Cases with changed steps | Net steps |
|---|---|---|---|
| D20 (L2 timing) | 0 | 160 | +167 |
| D21 | 0 | 0 | 0 (alone, without D20: +2, both also found by D20) |
| D22 | 1: case_55 COMPLETE→INCOMPLETE | 1 | +3 |
| D13 revised | 1: case_63 COMPLETE→INCOMPLETE | 1 | +3 |

### 4.6 Round 3: policy conditions (audit, then D23)
**Finding.** The pipeline never read the `condition` column of `deletion_rules.csv`. rule_07 was evaluated implicitly through the
analytics state; rule_05/06/12/13/14/15 were assumed met; CONTRACT §6 claimed a `retention_until > T` check that the code did not make.
The full audit is in `policy_coverage.md`, with 2 audited examples per category and branch.

**Refuted hypothesis.** "The hidden answers differentiate on expired retention or holds." Every retention/hold end date in the
participant data is in 2027–2034 (audit records 2031–2034, one PDF 2027-08-25, legal holds 2033-12-31), after every `audit_at`.

**Fix (D23, for correctness, not score).**
- The stated end date (`retention_until`, `retained_until`, PDF "retain until") is parsed in every parser.
- rule_12/13 now have explicit met / not met / undeterminable branches: not met → DELETE; undeterminable is assumed met and logged
  per resource.
- Eval branch counts: rule_12 99 met / 323 undeterminable; rule_13 5 met / 3 undeterminable.
- `output.csv` is byte-identical before and after.

**D25 check.** All 7 tuning resources labelled SUPPORT_MESSAGE are REDACT. The 3 deleted support records are 2 attachments plus
`support_002`, a contact-index entry with no record type.

### 4.7 Phase 4: freeze
- **Freeze:** `code/config/frozen_v1.toml` pins models, prompt hashes, thresholds and flags; `main.py` verifies it against the code before running.
- **Cache-only:** a cache miss aborts the run, so no keyword fallback and no live call can happen.
- **Tracked cache:** the 17 responses needed (16 production, 1 evaluation) moved to the tracked `code/llm_cache/`, so a clean checkout
  reproduces `output.csv` without an API key.
- **D27:** a subject with no observable record is an explicit, logged branch and a validator warning.

## 5. A/B comparisons

### 5.1 Timing links (D3 → D15 → D20)
| Run | Split | Status | P | R | F1 | Sequence | Wrong timing links |
|---|---|---|---|---|---|---|---|
| Round 1, timing off | tuning | 100.00% | 99.24% | 87.25% | 92.86% | 33.33% | – |
| Round 1, timing on (request window, per-case follow-ups) | tuning | 100.00% | 97.14% | 91.28% | 94.12% | 37.50% | 3 |
| **L2 (D20)** | tuning | 100.00% | **99.30%** | **94.63%** | **96.91%** | **62.50%** | **0** |
| Round 1, timing off / on | holdout | 100.00% | 100 / 100 | 81.82 / 81.82 | 90.00 / 90.00 | 37.50% | – |
| **L2 (D20)** | holdout | 100.00% | **100.00%** | **90.91%** | **95.24%** | **62.50%** | **0** |

L2 ablations (tuning, link-level; counts include links also made by identifiers):

| Variant | Links correct / wrong |
|---|---|
| **L2** | **29 / 0** |
| request window `[requested_at, reported_at]` instead of schedule windows | 22 / 1 |
| without the one-per-type slot rule | 24 / 1 |
| without the account result filter | 24 / 0 |
| strict PARTIALLY_COMPLETE → PARTIAL only | 28 / 0 |
| plus closed-world guard | no change (not adopted, D20) |

Label-free calibration of the windows (worker events on resources linked by identifiers in the 518 eval cases, minutes from `reported_at`):

| Event type | n | min | p1 | p99 | max | L2 window |
|---|---|---|---|---|---|---|
| account DELETE | 236 | −41232.3 | −74.8 | −44.0 | 272936.8 | [−80, +10] |
| REDACT_RECORD (follow-up) | 37 | −28.8 | −28.8 | −14.9 | −14.9 | [−35, 0) |
| RETAIN_RECORD (follow-up) | 8 | −29.0 | −29.0 | −28.1 | −28.1 | [−35, 0) |
| DISABLE_DATA_FLOW (follow-up) | 33 | −15.0 | −15.0 | −7.4 | −7.4 | [−35, 0) |
| BLOCK_RESTORE (follow-up) | 33 | −8.0 | −8.0 | −2.9 | −2.9 | [−35, 0) |
| DELETE / REDACT / ANONYMIZE "final result" | 525 | | 38.2+ | 133.0 | | outside both windows |

The two account outliers belong to a different request by the same person (`account_5762`: a failed attempt four weeks before case_34;
`account_6523`: deleted six months after case_46). They do not argue for a wider window.

### 5.2 Snapshot (absence) rules
Tuning split, keyword intent, no screenshots, so only the rule differs:

| Absence rule | State-caused missing steps | P | R | F1 | Sequence |
|---|---|---|---|---|---|
| override (baseline): the latest snapshot omission wins | 12 | 97.39% | 75.17% | 84.85% | 16.67% |
| (a) scope-aware: omission counts only inside the snapshot's store/collection | 10 | 98.29% | 77.18% | 86.47% | 20.83% |
| (b) fallback: omission counts only without an explicit observation `<= audit_at` | 4 | 98.37% | 81.21% | 88.97% | 29.17% |
| **(c) = (b), only for DELETE-policy resources, + fix 2 (D19)** | **0** | **99.21%** | **83.89%** | **90.91%** | **33.33%** |

(a) is not enough: evidence_51 declares the same store as the explicit observation and is partial *within* it. (c) reflects that omission
can show a record was deleted, never that a retained or redacted record is gone.

### 5.3 Citation rules for absence
All 8 absence citations in the tuning gold (`code/evaluation/citation_rules.py`):

| Rule | Fit |
|---|---|
| R1 latest file `<= audit_at` showing absence | 4/8 |
| R2 earliest file after the delete event | 0/8 |
| R3 same file as the case's other steps | 6/8 |
| **R4 latest explicit absence observation (404 / NOT_FOUND / rows=0); omission snapshot only if none** | **8/8** |

### 5.4 Screenshot model comparison (D17)
11 PNGs. The cross-check is the catalog activities (activity_4000–4011); these are claims, and the correct reading of
`marketing_profile_console.png` disagrees with its claim.

| Model | Printed resource ids found (of 9) | Identity agrees with catalog | Records | Flagged invalid values | Input / output tokens | Est. cost |
|---|---|---|---|---|---|---|
| **gemini-3.5-flash-lite** (chosen) | **9/9** | 7/11 | 37 | 14 | 15,965 / 8,322 | **$0.026** |
| gemini-2.5-flash | 8/9 (put `support_1034` in the user_id field, rejected by validation) | 7/11 | 67 | 20 | 6,919 / 37,125 | $0.095 |

### 5.5 LLM vs keyword rules
| Comparison | Tuning status | Tuning P / R / F1 | Holdout | Eval (518) |
|---|---|---|---|---|
| **Frozen run (LLM intent + screenshots)** | **100.00%** | **99.30 / 94.63 / 96.91** | 100 / 90.91 | reference |
| Keyword intent, no screenshots (`--no-llm`) | 95.83% | 99.27 / 91.28 / 95.10 | 100 / 90.91 (unchanged) | 8 cases INCOMPLETE → COMPLETE (their findings come only from screenshots); 10 cases change steps |

Intent alone: across all 550 request rows (398 unique texts), the LLM and the keyword rules disagree on exactly 1 text (case_26,
"confirm that the profile is gone", which the LLM correctly reads as a deletion request).

## 6. Participant evaluation cases (E1–E5)
Runnable: `python3 code/evaluation/custom_cases.py`. Inputs, expected output and the pass/fail check are in the file. The pipeline runs
unchanged on synthetic request rows over the real dataset, with the frozen config, cache-only.

| Case | Request (synthetic row) | What it proves | Expected | Actual | Result |
|---|---|---|---|---|---|
| E1 | kwame-mensah@proton.me: "Please remove me from marketing lists only. Keep my account and orders." | a deletion keyword with partial scope is not a deletion request | NOT_APPLICABLE, [] (marketing_001 is linked to this subject in case_63) | NOT_APPLICABLE, [] | PASS |
| E2 | amara-okeke@proton.me: "No quiero eliminar mi cuenta; solo necesito una copia de mis datos." | negation in another language | NOT_APPLICABLE, [] | NOT_APPLICABLE, [] | PASS |
| E3 | minjun.kim90@gmail.com: "電話番号を更新してください。アカウントは削除しないでください。" | historical identity + correction request | NOT_APPLICABLE, []; resolves to user_12 (historical) | as expected | PASS |
| E4 | case_153 with `audit_at` 1 s before evidence_15 | inclusive cutoff; later evidence never cited | no step cites evidence_15; account_8023 not reported from it | COMPLETE, [] (logged NO_OBSERVABLE_RECORDS, D27) | PASS |
| E5 | g.silva76@gmail.com (case_35 row) | near-miss decoy rejection + screenshot extraction | search_428 reported from screenshot evidence_09; decoy search_1874 (g.silva76.jr) in no step | INCOMPLETE; REPORT_REMAINING_RECORD search_428 (evidence_09); no search_1874 | PASS |

E1–E3 needed 1 extra intent call, in its own batch so the production batches stayed valid. Its response is cached, so re-runs cost 0 (`usage_report.md`).

## 7. Known limitations
- **Interchangeable twin resources (7 tuning steps, D29).** When several requests share a `reported_at` slot and their resources carry no
  identifiers anywhere, the data cannot tell them apart: `account_410` {case_23, 547, 548}, `account_7012` {28, 29}, `analytics_9406` {03, 44},
  `analytics_5335` {21, 23, 546, 547, 548}, `analytics_1895`/`analytics_4566` {24, 25, 26, 549, 550}, `analytics_4574` {27, 28}. They stay unlinked.
- **Unreachable gold (1 tuning step, 1 holdout step):** `audit_record_6645` and `audit_record_5569` are in no participant file.
- **Known extra step (D28):** `account_6523` in case_17. It belongs to the same person's earlier request (case_46); see §8.
- **No observable records (D27).** A deletion subject with nothing observable `<= audit_at` gets COMPLETE with `[]`. This is logged as
  `NO_OBSERVABLE_RECORDS` and flagged by the validator. 0 of 518 eval rows hit it (E4 does, by construction).
- **Assumed conditions.** 323 audit records and 3 legal holds have no stated end date, so rule_12/13 are assumed met (logged, D23).
  rule_05 (shared context) and rule_14 (restore can be blocked) are assumed met (D25); rule_15 is satisfied by the tombstone's existence (D24).
- **Rules never exercised on eval:** rule_02, 04, 08, 11 (no linked resource of those categories).
- **Screenshot Owner column.** The model reads Owner emails into attributes, but `vision.record_identifiers` ignores them. A zero-call replay
  that uses them changes 0 eval and 0 sample outputs, so it was not changed (D26).
- **Activity-only proposals without observation** (`object_19`, `tombstone_17`) produce no step (D14).
- **Frozen to this dataset's cache.** A new dataset needs live calls (`--live`, a key in `.env`) to fill the cache for its texts and screenshots.

## 8. Rejected decisions
| Decision | Proposal | Why rejected |
|---|---|---|
| D28 | Request scoping: a resource seen only before a person's earlier request belongs to that request (would drop `account_6523` from case_17) | It fixes 1 tuning step and changes 0 eval outputs. A rule whose only effect is matching a sample is fitting, not generalizing. |
| D29 | Deterministic tie-break that assigns each interchangeable twin resource to one candidate request | It could raise sequence accuracy on about half of those cases, but an audit tool that attributes another person's data to a requester is wrong by design. |
| D20 | Closed-world guard in L2 (account events of people without a request block follow-up links) | No measured effect; it would only add complexity. |
| D13 (original) | Veto a catalog link whenever an observation shows a different identity | It left case_63 as an unprovable empty COMPLETE; revised to veto only identities that resolve to another known user. |
| Round 1 | Scope-aware absence rule (a) and fallback rule (b) | Measured worse than (c) (§5.2). |
| D17 | gemini-2.5-flash for screenshots | Lower id recall, more noise, 3.6× the cost (§5.4). |

## 9. Token usage
The final run made **0 live API calls**; its usage is that of the 16 original calls behind the cached responses (`usage_report.md`).
Development calls, for context:

| Model | Task | API calls | Input tokens | Output tokens | Est. cost (list price) |
|---|---|---|---|---|---|
| gemini-3.6-flash | intent (unbatched, abandoned at the free-tier quota) | 16 | 5,313 | 4,049 | $0.019 |
| gemini-2.5-flash | intent (5 batches, **production**) | 5 | 19,480 | 29,246 | $0.079 |
| gemini-2.5-flash | screenshot comparison | 11 | 6,919 | 37,125 | $0.095 |
| gemini-3.5-flash-lite | screenshots (**production**) | 11 | 15,965 | 8,322 | $0.026 |
| gemini-2.5-flash | intent, custom cases E1–E3 (evaluation) | 1 | 384 | 667 | $0.002 |
| **total** | | **44** | 48,061 | 79,409 | **$0.221** |
