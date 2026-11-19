"""English-only policy for user questions (NFR-7 hardening, 2026-11-19).

The corpus, the prompts and both answer models are English, and most prompt-injection misses in other languages
(PRD §9.1b L21) disappear if non-English questions are refused politely. Offline, no extra dependency:

  * a letter from a non-Latin, non-Greek script (Devanagari, CJK, Cyrillic, Arabic, ...) -> not English
    (Greek is allowed: alpha-helix, Delta G);
  * a message of 4+ words where fewer than half the words are English (the symspellpy English dictionary the
    typo normaliser already uses, or a thesis-title term), or with 2+ common function words of other languages
    and fewer than 80 % English words -> not English;
  * shorter messages always pass (too little evidence; the injection fence still checks them).

Measured 2026-11-19: 0/1,438 English research questions refused (retrieval-evaluation set, all hard negatives and
edge cases such as El Niño, Le Chatelier, DES, the del operator); 1,574/1,590 translated questions/attacks in six
languages and 104/104 genuine round-4 questions in nine languages refused. Switch off with ENGLISH_ONLY=0.
"""

from __future__ import annotations

import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from pipeline.sanitizer import PromptInjectionAlert

ENGLISH_ONLY = os.getenv("ENGLISH_ONLY", "1") != "0"
ENGLISH_ONLY_MESSAGE = ("AskIISc answers questions in English only. Please ask your question in English.")
MIN_WORDS = 4
MIN_ENGLISH_SHARE = 0.5
FOREIGN_WORDS_LIMIT = 2
FOREIGN_SHARE_CEILING = 0.8

_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")
_TERMS_PATH = Path(__file__).resolve().parent.parent / "models" / "spell" / "thesis_title_terms.txt"
_FOREIGN = frozenset("""
les une est sont avec dans qui que quel quelle quels quelles comment pourquoi votre vos nous vous sur pas aux
ces cette der das und ist nicht mit für auf sind wie welche welcher welches warum ich sie ihr ihre ihnen eine einen
einem zu von bei oder auch können kann wird werden los las una es en con por como qué cuál cuáles porque usted
ustedes sus está están puede pueden il gli della delle degli che sono quale quali perché nel nella sul alla dei het
een zijn welke waarom niet voor ik je jij uw não você vocês qual quais são com pelo pela das nas
""".split())   # common function words of fr/de/es/it/nl/pt; English or technical tokens (die, son, come, pour, des =
               # DES, del = the operator, para- (chemistry), op-amp, DOS, NAS, la/el/le as in La Niña, El Niño,
               # Le Chatelier) deliberately left out; counted only when written in lowercase (names are capitalised)


class LanguageNotSupported(PromptInjectionAlert):
    """The question is not in English (handled like a fence rejection: HTTP 400, with its own message)."""


@lru_cache(maxsize=1)
def _terms() -> frozenset:
    try:
        return frozenset(l.split()[0].lower() for l in _TERMS_PATH.read_text().splitlines() if l.strip())
    except OSError:
        return frozenset()


def _non_latin(text: str) -> bool:
    for ch in text:
        if unicodedata.category(ch).startswith("L"):
            name = unicodedata.name(ch, "")
            if "LATIN" not in name and "GREEK" not in name:
                return True
    return False


def is_english(text: str) -> bool:
    from pipeline.text_processing.typo_normalizer import _is_english
    if _non_latin(text):
        return False
    raw = [w for w in _WORD.findall(text) if len(w) >= 2]
    words = [w.lower() for w in raw]
    if len(words) < MIN_WORDS:
        return True
    terms = _terms()
    english = sum(_is_english(w) or w in terms or w.rstrip("s") in terms for w in words) / len(words)
    foreign = sum(w in _FOREIGN for w in raw)   # lowercase occurrences only
    return not (english < MIN_ENGLISH_SHARE or (foreign >= FOREIGN_WORDS_LIMIT and english < FOREIGN_SHARE_CEILING))


def check_english(text: str) -> None:
    if ENGLISH_ONLY and not is_english(text or ""):
        raise LanguageNotSupported(ENGLISH_ONLY_MESSAGE)
