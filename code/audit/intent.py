"""Does the request text authorize deletion? (S2, D1)

The LLM classifier (IntentClassifier, batched and cached) decides. The keyword rules below are only a fallback: they
are used when the API fails, when a batch fails its exact-id check, or with --no-llm. Every decision carries its
source, so the run log shows whenever the fallback was used.

Decisions: D1 (LLM intent, cached, keyword fallback only on API failure), D18 (batched, exact-id check, disagreements logged).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

KEYWORD_FALLBACK_LABEL = "keyword_fallback"

# Deletion verbs/nouns in the six request languages seen in the dataset (en, es, pt, fr, ja, ko).
DELETION = re.compile(
    r"\b(delet\w*|eras\w*|remov\w*|clos(e|ing)\s+(my|this|the)\s+account|close\s+my\s+account|"
    r"eliminar|eliminaci[oó]n|borr\w+|exclus[aã]o|excluir|apagar|suppression|supprimer|effacer)\b"
    r"|削除|삭제",
    re.IGNORECASE,
)
# Phrases that negate or narrow the request to a non-deletion action.
NON_DELETION = re.compile(
    r"\b(not|don'?t|do not|never|without)\b[^.?!]{0,40}\b(delet\w*|eras\w*|remov\w*|clos\w*|change anything)"
    r"|\b(not asking for deletion|not a request to erase|marketing lists? only|only\b[^.?!]{0,30}\bmarketing)"
    r"|\bleave (the|my) account active\b|\bkeep (the|my) account\b|\bcontinue using the service\b"
    r"|\bno (quiero|deseo) (eliminar|borrar)|\bnão (quero|desejo) (excluir|apagar)|\bne (veux|souhaite) pas\b[^.?!]{0,30}supprim"
    r"|削除しないで|삭제하지",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Intent:
    authorizes_deletion: bool
    source: str
    rationale: str


def keyword_intent(text: str) -> Intent:
    neg = NON_DELETION.search(text)
    pos = DELETION.search(text)
    if neg:
        return Intent(False, KEYWORD_FALLBACK_LABEL, f"non-deletion phrase {neg.group(0)!r}")
    if pos:
        return Intent(True, KEYWORD_FALLBACK_LABEL, f"deletion phrase {pos.group(0)!r}")
    return Intent(False, KEYWORD_FALLBACK_LABEL, "no deletion phrase found")


def classify(text: str) -> Intent:
    return keyword_intent(text)


# --- LLM intent (D1, D17). The keyword fallback above is used only when the API call fails. ---------------------

PROMPT_PATH = __import__("pathlib").Path(__file__).resolve().parents[1] / "prompts" / "intent.md"
INTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "authorizes_deletion": {"type": "BOOLEAN"},
        "request_type": {"type": "STRING", "enum": ["deletion", "deletion_verification", "access", "export",
                                                   "correction", "marketing_opt_out", "account_support", "other"]},
        "rationale": {"type": "STRING"},
    },
    "required": ["authorizes_deletion", "request_type", "rationale"],
}


BATCH_SCHEMA = {
    "type": "OBJECT",
    "properties": {"results": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {"id": {"type": "INTEGER"}, **INTENT_SCHEMA["properties"]},
        "required": ["id", "authorizes_deletion", "request_type", "rationale"]}}},
    "required": ["results"],
}
BATCH_SIZE = 80


class IntentClassifier:
    """LLM intent, classified in deterministic batches (free-tier quota: 20 requests/day/model).

    prepare() classifies the union of all request texts in fixed batches (texts ordered by sha256, BATCH_SIZE per
    call), so every run reuses the same cached batches. A text missing from a valid batch answer falls back to the
    keyword rules, labelled in the run log.
    """

    def __init__(self, client=None):
        self.client = client
        self.system = PROMPT_PATH.read_text(encoding="utf-8") + (
            "\n\nYou receive several numbered requests. Classify each one independently and return one result per id. "
            "Keep each rationale under 15 words.")
        self.results: dict[str, Intent] = {}

    def prepare(self, texts: Iterable[str]) -> list[str]:
        """Classify all texts in batches; returns log lines."""
        if self.client is None:
            return []
        import hashlib

        from llm import LLMError

        log = []
        ordered = sorted(set(texts), key=lambda t: hashlib.sha256(t.encode()).hexdigest())
        for b in range(0, len(ordered), BATCH_SIZE):
            batch = ordered[b:b + BATCH_SIZE]
            prompt = "\n\n".join(f"[{i}]\n<<<\n{t}\n>>>" for i, t in enumerate(batch, 1))
            try:
                res = self.client.generate("intent", prompt, system=self.system, json_schema=BATCH_SCHEMA, temperature=0.0)
                returned = [int(r["id"]) for r in res.json()["results"]]
                items = {int(r["id"]): r for r in res.json()["results"]}
            except (LLMError, ValueError, KeyError, TypeError) as e:
                log.append(f"intent batch {b // BATCH_SIZE + 1}: API/parse failure ({type(e).__name__}: {str(e)[:100]}); keyword fallback")
                continue
            # D18: the response must contain exactly the input ids (no missing, extra or duplicate ids),
            # otherwise the whole batch is rejected to rule out position mix-ups.
            expected = set(range(1, len(batch) + 1))
            dupes = sorted({i for i in returned if returned.count(i) > 1})
            if set(returned) != expected or dupes:
                log.append(f"intent batch {b // BATCH_SIZE + 1}: id check FAILED (missing {sorted(expected - set(returned))[:5]}, "
                           f"extra {sorted(set(returned) - expected)[:5]}, duplicates {dupes[:5]}); whole batch -> keyword fallback")
                continue
            log.append(f"intent batch {b // BATCH_SIZE + 1}: id check passed ({len(batch)} ids)")
            src = f"llm:{res.provider}:{res.model}:batch{b // BATCH_SIZE + 1}" + (":cache" if res.cached else "")
            for i, t in enumerate(batch, 1):
                r = items.get(i)
                if r is None:
                    log.append(f"intent batch {b // BATCH_SIZE + 1}: no answer for item {i}; keyword fallback")
                    continue
                self.results[t] = Intent(bool(r["authorizes_deletion"]), src,
                                         f"{r.get('request_type')}: {str(r.get('rationale', ''))[:160]}")
        return log

    def disagreements(self, rows: list[dict]) -> list[str]:
        """D18: texts where the LLM and the keyword rules disagree, for manual review (rows: request dicts)."""
        by_text: dict[str, list[str]] = {}
        for r in rows:
            by_text.setdefault(r["request_text"], []).append(r["case_id"])
        out = []
        for text, cases in by_text.items():
            llm, kw = self.results.get(text), keyword_intent(text)
            if llm and llm.authorizes_deletion != kw.authorizes_deletion:
                out.append(f"intent disagreement {','.join(cases)}: llm={llm.authorizes_deletion} ({llm.rationale[:70]}) "
                           f"keyword={kw.authorizes_deletion} ({kw.rationale}) | {text[:90]}")
        return out

    def classify(self, text: str) -> Intent:
        if text in self.results:
            return self.results[text]
        fb = keyword_intent(text)
        if self.client is None:
            return fb
        return Intent(fb.authorizes_deletion, "keyword_fallback(no LLM answer)", fb.rationale)
