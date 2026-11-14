"""Spelling correction and base words for short topic questions (keyword-assisted retrieval).

Libraries:
  * symspellpy  - spell checker (edit distance first, then word frequency). Dictionary = the English
                  frequency list shipped with symspellpy + thesis-title words from
                  models/spell/thesis_title_terms.txt (scripts/build_spell_dictionary.py), boosted so
                  corpus terms (gmres, perovskite, transformer) win ties. Titles are clean metadata, so
                  OCR junk from thesis bodies ("transfomers", "graphen") is never a correction target.
  * lemminflect - lemmas and inflections: transformers -> transformer (and back), batteries -> battery,
                  without merging different words the way a stemmer does (transformation stays).

Both are optional: without them, no correction is attempted and simple plural rules are used.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("pipeline.topic_speller")

ROOT = Path(__file__).resolve().parent.parent.parent
TITLE_TERMS = ROOT / "models" / "spell" / "thesis_title_terms.txt"
TITLE_BOOST = 10 ** 9          # per thesis: comparable to common English word counts
MAX_EDIT_DISTANCE = 2
MIN_LEN_TO_CORRECT = 4


class TopicSpeller:
    _instance: Optional["TopicSpeller"] = None
    _lock = threading.Lock()

    def __init__(self):
        self.sym = None
        self.title_terms: Dict[str, int] = {}
        try:
            from symspellpy import SymSpell
            from importlib.resources import files
            sym = SymSpell(max_dictionary_edit_distance=MAX_EDIT_DISTANCE, prefix_length=7)
            sym.load_dictionary(str(files("symspellpy") / "frequency_dictionary_en_82_765.txt"), 0, 1)
            if TITLE_TERMS.exists():
                for line in TITLE_TERMS.read_text().splitlines():
                    word, _, n = line.partition(" ")
                    if word and n.isdigit():
                        self.title_terms[word] = int(n)
                        sym.create_dictionary_entry(word, int(n) * TITLE_BOOST)
            else:
                logger.warning("Spelling dictionary %s missing; run scripts/build_spell_dictionary.py", TITLE_TERMS)
            self.sym = sym
        except Exception as exc:  # library missing or dictionary unreadable: degrade gracefully
            logger.warning("Topic spell correction disabled: %s", exc)
        try:
            from lemminflect import getAllInflections, getLemma
            self._get_lemma, self._get_infl = getLemma, getAllInflections
        except Exception as exc:
            logger.warning("lemminflect unavailable, using simple plural rules: %s", exc)
            self._get_lemma = self._get_infl = None

    @classmethod
    def get_instance(cls) -> "TopicSpeller":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ---------------------------------------------------------------- base words
    def lemma(self, word: str) -> str:
        if self._get_lemma:
            lem = self._get_lemma(word, upos="NOUN")
            if lem:
                return lem[0].lower()
        if word.endswith("ies") and len(word) > 4:
            return word[:-3] + "y"
        if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
            return word[:-1]
        return word

    def variants(self, word: str) -> List[str]:
        """The word, its lemma and the lemma's singular/plural forms (no stemming)."""
        lem = self.lemma(word)
        forms = {word, lem}
        if self._get_infl:
            for vals in self._get_infl(lem, upos="NOUN").values():
                forms.update(v.lower() for v in vals)
        else:
            forms.add(lem + "s")
        return sorted(f for f in forms if f.isalnum())

    # ---------------------------------------------------------------- spelling
    def known(self, word: str) -> bool:
        if self.sym is None:
            return True
        return any(w in self.title_terms or w in self.sym.words for w in (word, self.lemma(word)))

    def correct(self, word: str) -> Optional[str]:
        """Return a corrected word for an unknown topic word, else None."""
        if self.sym is None or len(word) < MIN_LEN_TO_CORRECT or not word.isalpha() or self.known(word):
            return None
        from symspellpy import Verbosity
        for s in self.sym.lookup(word, Verbosity.CLOSEST, max_edit_distance=MAX_EDIT_DISTANCE):
            if s.term != word and s.term[0] == word[0]:
                return s.term
        return None

    def correct_terms(self, terms: List[str]) -> Tuple[List[str], Dict[str, str]]:
        out, fixes = [], {}
        for t in terms:
            fix = self.correct(t)
            if fix:
                fixes[t] = fix
            out.append(fix or t)
        return out, fixes
