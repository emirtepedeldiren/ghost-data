"""Does the request text authorize deletion? (S2, D1)

TEMPORARY: until the LLM intent model is approved (D10), every request goes through the keyword
fallback below. D1 keeps this fallback only for API failures once the LLM path exists. Each decision
is labelled with its source so the run log shows that the fallback was used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TEMPORARY_FALLBACK_LABEL = "keyword_fallback(TEMPORARY: LLM intent pending approval)"

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
        return Intent(False, TEMPORARY_FALLBACK_LABEL, f"non-deletion phrase {neg.group(0)!r}")
    if pos:
        return Intent(True, TEMPORARY_FALLBACK_LABEL, f"deletion phrase {pos.group(0)!r}")
    return Intent(False, TEMPORARY_FALLBACK_LABEL, "no deletion phrase found")


def classify(text: str) -> Intent:
    return keyword_intent(text)
