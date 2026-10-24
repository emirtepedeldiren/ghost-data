# Ghost Data Still Exists: deletion-audit agent

HackerRank Orchestrate, October 2026. For each request in `dataset/deletion_requests.csv`, the agent decides whether the person
asked for deletion. If they did, it finds every current and historical identifier, reconstructs the state of each linked
record at `audit_at` from the evidence, applies `deletion_rules.csv`, and writes the findings, ordered remediation and
verification steps to `output.csv` (`case_id,deletion_status,steps_json`).
The full challenge contract is in [`problem_statement.md`](./problem_statement.md).

## Quick start (one command)

Requirements: Python 3.11+ (tested with 3.13) and the participant `dataset/` folder at the repository root.

```bash
python3 -m venv .venv && .venv/bin/pip install -r code/requirements.txt && .venv/bin/python code/main.py
```

This writes `output.csv` (518 rows) at the repository root. **No API key and no network are needed.** The run uses the frozen
configuration `code/config/frozen_v1.toml` and reads every LLM response from the committed cache `code/llm_cache/` (cache-only:
a cache miss aborts the run instead of calling the API). The run prints the config sha256 and writes a run log and a per-case
decision log to `.work/runs/latest/`.

Windows (PowerShell): `py -m venv .venv; .venv\Scripts\pip install -r code\requirements.txt; .venv\Scripts\python code\main.py`

Expected result: config sha256 `58e1afb22468840b7d0f567291d1b684a6c1ab6cb4fa3270e528d2f566264604`, `output.csv` sha256
`774aa9ca3e0073383fc9251ce17217b684c4b06161c5385c712945a420d1cb1f`.

### Other commands
| Purpose | Command |
|---|---|
| Validate the output (structure, ordering, evidence rules, cutoff) | `.venv/bin/python code/validate.py --output output.csv` |
| Score the 32 samples (tuning / holdout split) | `.venv/bin/python code/main.py --requests dataset/sample_deletion_requests.csv --output .work/samples.csv` then `.venv/bin/python code/evaluation/main.py --pred .work/samples.csv --split tuning` (or `holdout`, `all`) |
| Participant evaluation cases E1–E5 | `.venv/bin/python code/evaluation/custom_cases.py` |
| Unit tests | `.venv/bin/python -m unittest discover -s code/tests` |
| Policy-condition coverage audit | `.venv/bin/python code/evaluation/policy_coverage.py --run latest` |
| Regenerate the usage report | `.venv/bin/python code/tools/usage_report.py` |
| Switch one decision off for measurement | e.g. `--no-timing-links`, `--no-unblocked-backup-rule`, `--strict-catalog-veto` (the run log records the deviation) |
| Live LLM calls for new data (development only) | put `GEMINI_API_KEY=...` in an uncommitted `.env`, then add `--live` |

## Architecture

```
dataset/*.csv ──► S1 load ──► S2 intent ─────────────── LLM (cached): does the text authorize deletion?
                               │ NOT_APPLICABLE → []
                               ▼
                  S3 identity: current + historical emails / phones / hashes / user_id (user_audit_log ≤ audit_at)
                               │
dataset/evidence/ ──► S4 extract: one Observation schema from every format
   .sql (sqlite3) .json .log (6 dialects, trace joins) .txt .pdf (text layer)
   .png ─────────────────────── LLM (cached): screenshot → structured records, ids validated
                               │
                               ▼
                  S5 link: identifier co-occurrence (≤ audit_at; post-audit files for ownership only),
                           catalog claims (vetoed by a conflicting known identity),
                           worker-schedule timing rule L2 (global, unique assignments only)
                               │
                               ▼
                  S6 state at audit_at: latest observation ≤ audit_at; snapshot omission proves ABSENT only
                           for DELETE-policy records with no explicit observation; replicas are separate stores
                               │
                               ▼
                  S7 policy: deletion_rules.csv per category (retention/hold conditions evaluated)
                               │
                               ▼
                  S8 steps: FINDING → REMEDIATION → VERIFICATION, contract ordering, citations ≤ audit_at
                               │
                               ▼
                  S9 output.csv  ──►  S10 validator
```

Code: `code/main.py` (entry point and pipeline), `code/audit/` (identity, parsers, linking, state, policy and steps),
`code/llm/` (provider-agnostic client with disk cache and call log), `code/contract.py` (output contract and ordering),
`code/validate.py`, `code/evaluation/` (scorer, splits, root-cause and coverage tools, custom cases, reports).
The full specification is `code/CONTRACT.md`.

## Where the LLM is used, and why only there

The model provider is Google Gemini, with model IDs in config only. It is used in exactly two places:
1. **Intent (S2), `gemini-2.5-flash`.** Request texts are free text in several languages, with negations, partial scopes and
   correction or export requests. The 398 unique texts are classified in 5 deterministic batches. A batch whose answer does not
   contain exactly its input ids is rejected (D18). Prompt: `code/prompts/intent.md`.
2. **Screenshots (S4), `gemini-3.5-flash-lite`.** 11 PNG application screens are transcribed into records once. Every
   extracted id is validated against strict patterns. Prompt: `code/prompts/vision_extract.md`.

Everything else (identity, parsing, linking, state, policy, ordering) is deterministic code. Those steps need exact, auditable
rules and an evidence trail per step, not judgement calls. Responses are cached on disk with token counts, and the final
run is served from that cache (`code/evaluation/usage_report.md`).

## Key decisions (details in `code/CONTRACT.md`)

| # | Decision |
|---|---|
| D1 | Intent via an LLM classifier, temperature 0, cached; keyword rules only as a logged fallback |
| D2 | Screenshots via a vision model, extracted once, cached, ids validated |
| D3 | No explicit join exists for identifier-free resources; timing linking had to be earned with data (see D20) |
| D4 | Cite the latest evidence file captured ≤ audit_at that observes the claimed state |
| D5 | Category: explicit evidence first, then the worker's attempted operation, then the prefix default |
| D6, D7 | Strict operation order, then system_id, then resource_id; rows in numeric case_id order |
| D8 | Reports in `code/evaluation/` (bundled by the official zip command) |
| D9 | system_id as named in the evidence record; prefix map only as fallback |
| D10 | Provider-agnostic client; provider Gemini; keys only from the environment / `.env` |
| D11 | An activity row may *propose* a link; state and citations come only from evidence files |
| D12 | Files captured after audit_at may decide ownership, never state or citation |
| D13 | A catalog link is vetoed only when an observation shows an identity of a different known user |
| D14 | Activity-only proposals with no observation produce no step |
| D15 | The timing linker is an ordinary link source, defaulted by data |
| D16 | Mechanical holdout (every 4th sample); tuning on 24 cases only; contamination disclosed |
| D17 | Screenshot model chosen by comparison (`gemini-3.5-flash-lite`) |
| D18 | Intent on batched `gemini-2.5-flash` with an exact-id check; disagreements with the keyword rules logged |
| D19 | Absence rule (c): omission proves deletion only for DELETE-policy records; fix 2 verifications for evidence-linked resources |
| D20 | Linking rule L2: worker-schedule windows, account result filter, one resource per type, global unique assignment |
| D21 | Ownership from post-audit units; a step needs a state observation ≤ audit_at |
| D22 | A backup holding the subject's data with no observed block is a restore risk |
| D23 | Retention / legal-hold conditions evaluated from the stated end date; undeterminable is logged |
| D24 | A tombstone's existence satisfies "account is deleted" |
| D25 | Support messages without visible shared context stay REDACT |
| D26 | Free tier: the final run is cache-only, with 0 live calls |
| D27 | A subject with no observable record is an explicit `NO_OBSERVABLE_RECORDS` branch and a validator warning |
| D28 | Rejected: request scoping that would only fix one sample step |
| D29 | Rejected: tie-breaking interchangeable resources between requests (it would attribute another person's data) |

## Results

| Split | Status accuracy | Row accuracy | Step P / R / F1 | Field accuracy | Order on matched steps | Sequence accuracy |
|---|---|---|---|---|---|---|
| Tuning (24 samples) | 100.00% | 62.50% | 99.30 / 94.63 / 96.91% | 94.00% | 100% | 62.50% |
| Holdout (8 samples) | 100.00% | 62.50% | 100.00 / 90.91 / 95.24% | 90.91% | 100% | 62.50% |

The eval set (518 rows) passes the validator with 0 errors and 0 warnings. Details, A/B comparisons, failures and fixes round by round,
known limitations and rejected decisions: `code/evaluation/evaluation_report.md`. Tokens and cost: `code/evaluation/usage_report.md`.

## Packaging

From the repository root (the official command; it bundles `README.md` and `code/`, not `dataset/` or `.work/`):

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

Windows (PowerShell): `Compress-Archive -Path README.md, code -DestinationPath code.zip -Force` (remove `code\__pycache__` folders first).

Submission items: `code.zip`, the root `output.csv`, and `log.txt` (the agent session transcript, written per `AGENTS.md`).
Challenge page: https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
