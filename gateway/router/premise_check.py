"""Detect claim-checking ("loaded") questions that should be answered by the stronger Tier-2 model.

Red-team finding: questions such as "Why does X always ...?" or "Why can X ...?" embed a claim. The
3B CPU model tends to explain why the claim is true even when it is false; the 7B model, given the
same prompt and the same retrieved chunks, rejected the false premise in 9/9 measured cases. The MLP
router scores these short questions as easy (P(hard) ~ 0.01), so they are escalated by this rule.

A question is "loaded" when it asks WHY (incl. "how come", "explain why") AND asserts an absolute or
possibility claim (always / never / every / all / only / guarantee / must / cannot ..., or "why can ...").
"""

from __future__ import annotations

import re
from typing import Optional

_ABSOLUTE = {
    "always", "never", "every", "all", "each", "any", "only", "none", "nothing", "everything", "everyone",
    "nobody", "guarantee", "guarantees", "guaranteed", "must", "necessarily", "invariably", "impossible",
    "cannot", "completely", "entirely", "totally", "universally", "certainly", "definitely", "inevitably",
}
_POLITE = re.compile(
    r"^(?:(?:please|kindly)\s+|(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
    r"(?:i\s+(?:want|would\s+like|wish)\s+to\s+know)\s+|do\s+you\s+know\s+)+"
)
_INTRO = {"explain", "describe", "tell", "discuss"}


def _tokens(query: str):
    text = (query or "").lower().replace("’", "'").replace("‘", "'")
    text = _POLITE.sub("", text.strip())
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text)


def premise_check_reason(query: Optional[str]) -> Optional[str]:
    """Return a short reason if the question is a loaded 'why' claim, else None."""
    t = _tokens(query or "")
    if not t:
        return None
    if t[0] == "why":
        rest = t[1:]
    elif t[:2] == ["how", "come"]:
        rest = t[2:]
    elif t[0] in _INTRO and "why" in t[1:4]:
        rest = t[t.index("why") + 1:]
    else:
        return None
    hits = sorted({w for w in rest if w in _ABSOLUTE or (w.endswith("n't") and w not in ("isn't", "aren't", "wasn't", "weren't"))})
    if hits:
        return "absolute claim (" + ", ".join(hits) + ")"
    if rest and rest[0] in ("can", "could"):
        return "possibility claim ('why can ...')"
    return None
