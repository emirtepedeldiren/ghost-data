# Ghost Data Still Exists: deletion-audit agent

For each request in `dataset/deletion_requests.csv`, the agent decides whether the person asked for deletion, finds their
current and historical identifiers, reconstructs the state of every linked record at `audit_at` from the evidence, applies
`deletion_rules.csv`, and writes findings, ordered remediation and verification steps to `output.csv`.

## Quickstart

Python 3.11+ (tested on CPython 3.13.12), with the participant `dataset/` folder at the repository root. No API key is needed.

```bash
python3 -m venv .venv && .venv/bin/pip install -r code/requirements.txt     # setup (one dependency: pypdf)
.venv/bin/python code/main.py                                               # run: writes ./output.csv (518 rows)
.venv/bin/python -m unittest discover -s code/tests                         # test (identity, parsers, ordering, policy, compliance)
.venv/bin/python code/validate.py --output output.csv                       # validate the output contract
.venv/bin/python code/main.py --requests dataset/sample_deletion_requests.csv --output .work/samples.csv \
  && .venv/bin/python code/evaluation/main.py --pred .work/samples.csv --split tuning   # evaluate on the samples (or --split holdout / all)
.venv/bin/python code/evaluation/custom_cases.py                            # participant evaluation cases E1-E5
```

The run uses the frozen configuration `code/config/frozen_v1.toml` and is **cache-only**: every LLM response comes from the
committed cache `code/llm_cache/`, and a cache miss aborts the run instead of calling the API. Expected result: config sha256
`1f39d309dbb8d6611cd987feb0c577b3417642c005b615fbfbbeef91242ca612`, `output.csv` sha256
`774aa9ca3e0073383fc9251ce17217b684c4b06161c5385c712945a420d1cb1f`. Run and decision logs go to `.work/runs/<name>/`.
Windows: use `.venv\Scripts\python` in place of `.venv/bin/python`. Lint: `pip install -r code/requirements-dev.txt && ruff check code`.

## Architecture

```
dataset/*.csv ──► S1 load ──► S2 intent (LLM, cached) ── does the text authorize deletion? no → NOT_APPLICABLE, []
                                   │ yes
                                   ▼
                  S3 identity: current + historical emails / phones / hashes / user_id (user_audit_log)
                                   │
dataset/evidence/ ──► S4 extract: one Observation schema from .sql (sqlite3), .json, .log (6 dialects), .txt, .pdf (text layer)
                                  and .png (LLM transcription, cached, ids validated)
                                   │
                                   ▼
                  S5 link: identifiers in the same evidence unit; post-audit files for ownership only; catalog claims
                           (vetoed by a conflicting known identity); worker-schedule rule L2 (unique assignments only)
                                   │
                                   ▼
                  S6 state at audit_at: latest observation captured <= audit_at; omission proves ABSENT only for
                           DELETE-policy records without an explicit observation; replica tables are separate stores
                                   │
                                   ▼
                  S7 policy (deletion_rules.csv; retention / hold end dates evaluated) ──► S8 ordered steps ──► S9 output.csv ──► S10 validator
```

Principle: workflow statuses and activity rows are **claims**. They may propose a link, but state and citations come only from
evidence files captured at or before `audit_at`.

## Where to look

Full decision records are in `code/CONTRACT.md` (§14–§20); measurements are in `code/evaluation/evaluation_report.md`.

| Decision | What it does | Where (file:function) |
|---|---|---|
| D1, D18 | LLM intent classification, batched, exact-id check, keyword fallback only on API failure | `code/audit/intent.py:IntentClassifier.prepare`, `.classify` |
| D2, D17, D31 | Screenshots transcribed once by a vision model; ids validated; neutral prompt | `code/audit/vision.py:extract`, `code/prompts/vision_extract.md` |
| S3 | Current + historical identity, token-boundary matching, case-insensitive emails | `code/audit/identity.py:IdentityResolver.resolve` |
| D11 | Claims vs evidence: activity rows propose links, only files set state | `code/audit/linking.py:catalog_links`, `code/audit/reconstruct.py:StateIndex._state` |
| D12, D21 | Files captured after audit_at decide ownership, never state | `code/audit/linking.py:explicit_links`, `code/main.py:run` |
| D13 | Catalog link vetoed only by an identity that resolves to another known user | `code/audit/linking.py:catalog_links` |
| D20 | Rule L2: worker-schedule windows, one resource per type, global unique assignment | `code/audit/linking.py:TimingLinker` |
| D4, D19 | Latest observation <= audit_at; omission proves absence only for DELETE-policy records; fix 2 | `code/audit/reconstruct.py:StateIndex._state`, `build_steps` |
| D5, D9 | Category: explicit evidence, then the worker's operation, then the prefix default; system as named | `code/audit/reconstruct.py:StateIndex.category`, `code/audit/vocab.py:system_for` |
| D22 | A backup holding the subject's data with no observed block is a restore risk | `code/audit/reconstruct.py:StateIndex._state` |
| D23 | Retention / legal-hold end dates evaluated; undeterminable is logged | `code/audit/reconstruct.py:StateIndex.retention_condition` |
| D6, D7 | Phase → operation → system_id → resource_id ordering; rows in case_id order | `code/contract.py:step_sort_key`, `case_sort_key` |
| D26 | Frozen, cache-only run; config verified against the code | `code/main.py:load_frozen`, `code/llm/__init__.py:LLMClient.generate` |
| D27 | Subject with no observable record: explicit NO_OBSERVABLE_RECORDS branch | `code/main.py:run`, `code/validate.py:check_steps` |
| D16 | Mechanical holdout (every 4th sample), aggregates only | `code/evaluation/splits.py:holdout_ids` |
| D28, D29, D32 | Rejected: fitting one sample step; tie-breaking twins; record order as a signal (tested) | `evaluation_report.md` §8–§9 |
| Compliance | No hardcoded ids, gold never read, every input read, prompts free of dataset ids | `code/tests/test_core.py:NoHardcodedAnswers`, `PromptLeaks` |

## Where the LLM is used, and why only there

Provider: Google Gemini (model IDs in config only). The LLM is used in exactly two places:
1. **Intent (S2), `gemini-2.5-flash`.** Request texts are free text in several languages, with negations, partial scopes and
   access or export requests: that is reading comprehension, not a rule. The 398 unique texts go in 5 deterministic batches.
2. **Screenshots (S4), `gemini-3.5-flash-lite`.** 11 application screens must be read from pixels. Each is transcribed once,
   and every extracted id is validated against strict patterns.

Everything else (identity, parsing, linking, state, policy, ordering) is deterministic code: those steps need exact, auditable
rules and an evidence trail per step, not judgement. The final run makes 0 live calls; tokens and cost are in
`code/evaluation/usage_report.md`.

## Results

| Split | Status accuracy | Row accuracy | Step P / R / F1 | Field accuracy | Order on matched steps | Sequence accuracy |
|---|---|---|---|---|---|---|
| Tuning (24 samples) | 100.00% | 62.50% | 99.30 / 94.63 / 96.91% | 94.00% | 100% | 62.50% |
| Holdout (8 samples) | 100.00% | 62.50% | 100.00 / 90.91 / 95.24% | 90.91% | 100% | 62.50% |

On the 518 eval requests (no labels), the validator passes with 0 errors and 0 warnings, and for 165 of the 254 requests the
workflow reported COMPLETE, the audit finds personal data or a recreation path still present. Details, A/B comparisons and
round-by-round improvements: `code/evaluation/evaluation_report.md`.

## Known limitations

- **Interchangeable twin records.** Identifier-free records of requests that share a worker slot are left unlinked (D29). An
  investigation found no person-level field that separates them, and record order is not a signal (D32).
- **Weak absence proofs.** Absence by omission from replica tables only (64 eval accounts) is kept but unconfirmed (D30).
- **Assumed conditions.** Policy conditions that the evidence does not state (no retention date, no shared-context marker) are
  assumed met and logged (D23–D25).
- **Fixed cache.** The frozen run serves only this dataset's cached LLM responses; new data needs `--live` and a key in `.env`.
<!-- D31:readme -->
- **Screenshot prompt refresh pending.** The cached screenshot responses were produced by a superseded prompt kept in
  `code/prompts/legacy/`; `code/tools/refresh_vision.py` (D31) re-extracts them with the neutral prompt.
<!-- /D31:readme -->

Full list: `code/evaluation/evaluation_report.md` §8.

## Repository layout

```
README.md                 this file
code/main.py              entry point and pipeline (S1-S9)
code/audit/               identity, intent, evidence parsers, linking, state reconstruction, policy and steps
code/llm/                 provider-agnostic LLM client with disk cache, call log and cache-only mode
code/prompts/             intent and screenshot prompts (Markdown; hashes pinned in the frozen config)
code/config/              frozen_v1.toml: models, prompt hashes, thresholds, flags of the submitted run
code/llm_cache/           cached LLM responses the outputs depend on, with the original call records
code/contract.py          output contract and ordering; code/validate.py checks it
code/evaluation/          scorer, holdout split, analysis tools, custom cases, evaluation_report.md, usage_report.md
code/tools/               usage_report.py, prompt_leaks.py, refresh_vision.py; dev/ holds development-only tools
code/tests/               unit and compliance tests
code/CONTRACT.md          specification and decision record (D1-D32)
```

Packaging (official command): `zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"`.
Submission: `code.zip`, the root `output.csv`, and `log.txt` (agent transcript, per `AGENTS.md`).
Challenge page: https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
