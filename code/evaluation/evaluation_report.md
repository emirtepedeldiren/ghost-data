# Evaluation report: Ghost Data Still Exists audit agent

Status: **in progress (after Round 1, 2026-10-24)**. Sections marked TODO are filled in later rounds.

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

All 518 eval predictions pass the validator (2 empty-step warnings: case_55, case_63). `--timing-links` stays off by default
until Round 2 (linking) decides it from the data (D15).

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

### 3.5 Still open after Round 1 (tuning split)
19 missing gold steps: 10 resources linkable only through activity rows, 8 observed only without identifiers (404 lookups, anonymized
analytics), and 1 unreachable. There is 1 extra step (`account_6523`, a stale March lookup for case_17's email). This is the linking work for Round 2.

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
