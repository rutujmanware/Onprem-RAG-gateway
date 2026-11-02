"""Request-level safety checks required before cache access or generation."""

from __future__ import annotations

import re


INJECTION = re.compile(
    r"(?:ignore|disregard|override)\s+(?:all\s+)?(?:previous|system)\s+instructions|</?system>|"
    r"reveal\s+(?:the\s+)?(?:system\s+)?prompt",
    re.IGNORECASE,
)
PERSONAL = re.compile(
    r"\b(?:my|student(?:'s)?|roll(?:\s|-)?number|usn|grade|marks|transcript|fee\s+status|"
    r"application\s+status|personal\s+record)\b",
    re.IGNORECASE,
)


def is_prompt_injection(text: str) -> bool:
    return bool(INJECTION.search(text or ""))


def is_personalized_query(text: str) -> bool:
    return bool(PERSONAL.search(text or ""))
