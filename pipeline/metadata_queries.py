"""Requests that need metadata filtering (counting or listing theses by department, author, advisor, degree or
year) are not supported (DEFECT-33). Retrieval is content-based; such requests get a short, honest message
instead of an answer built from a handful of retrieved passages (which would look like a complete list or an
exact count). Content questions that merely mention a field ("mechanical properties", "2D materials") are not
affected.
"""

import re

METADATA_QUERY_MESSAGE = (
    "Counting or listing theses by department, author, advisor, degree or year (metadata filtering) is not "
    "supported yet. You can ask about a research topic instead, for example: \"What has been studied about "
    "perovskite solar cell stability?\"")

_NOUN = r"(?:theses|thesis|papers?|dissertations?|publications?|records|reports)"
_COUNT = re.compile(r"\b(?:how\s+many|number\s+of|count(?:\s+of)?|total(?:\s+number\s+of)?)\s+"
                    r"(?:\w+\s+){0,3}?" + _NOUN + r"\b", re.I)
_LIST = re.compile(r"\b(?:list|enumerate|show|give|find|name|get)\b(?:\s+\w+){0,4}?\s+(?:all\s+|every\s+)?" + _NOUN + r"\b"
                   r"|\b(?:all|every|which)\s+(?:\w+\s+){0,2}?" + _NOUN + r"\b"
                   r"|^\s*" + _NOUN + r"\b", re.I)
_FACET = re.compile(
    r"\bdepartments?\b|\bdept\.?\b"
    r"|\b(?:supervised|advised|guided|authored|written|submitted|published)\s+(?:by|in|under)\b"
    r"|\bunder\s+(?:prof|professor|dr)\b|\bby\s+(?:prof|professor|dr)\.?\s"
    r"|\b(?:advisor|supervisor|guide|author)s?\s+(?:is|was|named|called)\b"
    r"|\b(?:in|from|during|since|after|before|of)\s+(?:the\s+year\s+)?(?:19|20)\d\d\b"
    r"|\b(?:ph\.?\s?d|m\.?\s?tech|m\.?\s?sc|m\.?\s?e|master'?s|doctoral)\b(?=.*\b" + _NOUN + r"\b)", re.I)


def is_metadata_query(query: str) -> bool:
    """True for requests to count theses, or to list theses by department/author/advisor/degree/year."""
    q = query or ""
    if _COUNT.search(q):
        return True
    return bool(_LIST.search(q) and _FACET.search(q))
