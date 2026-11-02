"""Deterministic, on-premises baseline evaluator for grounded QA responses."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable


def _terms(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[a-zA-Z0-9]{3,}", text or "")}


class LocalJudge:
    """Reports lexical reference coverage and resolvable-citation presence."""

    def score(self, answer: str, reference: str, citations: Iterable[Dict[str, Any]]) -> Dict[str, float | bool]:
        reference_terms = _terms(reference)
        answer_terms = _terms(answer)
        coverage = len(reference_terms & answer_terms) / len(reference_terms) if reference_terms else 1.0
        citation_list = list(citations or [])
        citation_valid = bool(citation_list) and all(
            isinstance(item, dict) and bool(item.get("id")) and bool(item.get("doc_version"))
            for item in citation_list
        )
        return {"reference_term_coverage": round(coverage, 4), "citation_valid": citation_valid}
