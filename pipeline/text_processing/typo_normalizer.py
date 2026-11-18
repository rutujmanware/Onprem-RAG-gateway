#!/usr/bin/env python3
"""Ultra-Fast Institutional Typo Normalizer for AskIISc.

Corrects common misspellings in domain-specific academic, department, and technical
keywords (e.g., 'neroscience' -> 'neuroscience', 'vermiculitte' -> 'vermiculite')
with microsecond latency (< 0.05ms) using pre-indexed length buckets, character
intersection pruning, early-exit bounded Levenshtein distance, LRU caching, and
comprehensive valid English word protection (guaranteeing valid words like 'tell'
are never corrupted into 'cell').
"""

from __future__ import annotations

import functools
import logging
import os
import re
import threading
from collections import defaultdict
from typing import Dict, List, Optional, Set


logger = logging.getLogger(__name__)
_EN_LOCK = threading.Lock()
_EN_WORDS: Optional[Set[str]] = None


def _is_english(word: str) -> bool:
    """In the English frequency dictionary bundled with symspellpy (a project dependency)."""
    global _EN_WORDS
    if _EN_WORDS is None:
        with _EN_LOCK:
            if _EN_WORDS is None:
                words: Set[str] = set()
                try:
                    from importlib.resources import files
                    with open(str(files("symspellpy") / "frequency_dictionary_en_82_765.txt")) as fh:
                        words = {line.split()[0] for line in fh if line.strip()}
                except Exception as e:
                    logger.warning("English dictionary unavailable for typo protection: %s", e)
                _EN_WORDS = words
    return word in _EN_WORDS


_CORPUS_CLIENT = None
# A word counts as a real corpus term from this many chunks on. Measured: real terms 9-1323 chunks (stanene 9,
# fluxonium 12, graphane 18, ...); typos/extraction fragments 0-3 (nanoscienc 2, machne 3, catalist 1).
CORPUS_MIN_CHUNKS = int(os.getenv("TYPO_CORPUS_MIN_CHUNKS", "5"))


class CorpusUnavailable(RuntimeError):
    """The thesis index could not be asked; the decision must not be cached."""


@functools.lru_cache(maxsize=65536)
def _in_corpus(word: str) -> bool:
    """True if at least CORPUS_MIN_CHUNKS thesis chunks contain the word (verbatim). Raises CorpusUnavailable
    (not cached) if the index cannot be asked."""
    global _CORPUS_CLIENT
    if os.getenv("TYPO_CORPUS_PROTECTION", "1") == "0" or not re.fullmatch(r"[a-z]{4,40}", word):
        return False
    try:
        import redis
        if _CORPUS_CLIENT is None:
            from gateway.cache.redis_semantic_cache import DEFAULT_REDIS_URL
            _CORPUS_CLIENT = redis.Redis.from_url(os.getenv("REDIS_URL") or DEFAULT_REDIS_URL,
                                                  socket_connect_timeout=1, socket_timeout=2)
        index = os.getenv("REDIS_INDEX_NAME", "rag_documents_idx")
        res = _CORPUS_CLIENT.execute_command("FT.SEARCH", index, f"@content:({word})", "VERBATIM", "LIMIT", "0", "0")
        n = res[0] if isinstance(res, list) else (res.get(b"total_results", res.get("total_results", 0)) or 0)
        return int(n) >= CORPUS_MIN_CHUNKS
    except Exception as e:
        raise CorpusUnavailable(str(e)) from e


class TypoNormalizer:
    """Microsecond-latency typo corrector for academic and institutional vocabulary."""

    _instance: Optional["TypoNormalizer"] = None

    # Fallback valid English words to guarantee common words are protected
    # even in stripped environments lacking /usr/share/dict/words.
    FALLBACK_VALID_WORDS: Set[str] = {
        "tell", "cell", "call", "well", "will", "wall", "bell", "fell", "sell",
        "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
        "that", "this", "these", "those", "there", "their", "theirs", "here",
        "have", "has", "had", "having", "been", "being", "were", "with", "from",
        "into", "onto", "upon", "about", "above", "below", "under", "over",
        "give", "gives", "given", "giving", "show", "shows", "shown", "showing",
        "find", "finds", "found", "finding", "make", "makes", "made", "making",
        "take", "takes", "took", "taken", "taking", "come", "comes", "came",
        "good", "great", "best", "better", "more", "most", "less", "least",
        "some", "many", "much", "each", "every", "both", "all", "any", "none",
        "work", "works", "worked", "working", "study", "studies", "studied",
        "help", "helps", "need", "needs", "want", "wants", "know", "knows",
        "read", "reads", "look", "looks", "seem", "seems", "like", "such",
    }

    # Core institutional and academic vocabulary targets
    BASE_VOCABULARY: Set[str] = {
        # IISc Departments, Units & Disciplines
        "neuroscience", "nanoscience", "biophysics", "biochemistry", "materials",
        "aerospace", "mechanical", "chemical", "civil", "computational",
        "electronic", "electrical", "microbiology", "genetics", "atmospheric",
        "oceanic", "ecological", "astronomy", "astrophysics", "mathematics",
        "instrumentation", "management", "sociology", "communication", "automation",
        "sustainable", "technologies", "structural", "inorganic", "organic",
        "developmental", "molecular", "physical",

        # Scientific, Technical & Engineering Concepts
        "vermiculite", "magnetohydrodynamics", "algorithm", "algorithms",
        "optimization", "spectroscopy", "ferroelectric", "semiconductor",
        "semiconductors", "piezoelectric", "superconductivity", "crystallography",
        "turbulence", "instability", "convection", "combustion", "aerodynamics",
        "hypersonic", "nanomaterials", "plasmonic", "tribology", "biomaterials",
        "tuberculosis", "mycobacterium", "dielectric", "photovoltaic", "graphene",
        "perovskite", "polymer", "polymers", "catalysis", "catalyst", "dendritic",
        "neurons", "alzheimer", "depressive", "claudins", "fluorescence",
        "microscopy", "tomography", "thermodynamics", "diffraction", "epitaxy",
        "polarization", "kinetics", "equilibrium", "viscosity", "elasticity",
        "stoichiometry", "synthesis", "characterization", "spectrometry",
        "nanotechnology", "optics", "quantum", "plasmonics", "microfluidics",
        "electrochemical", "electromagnetics", "interferometry", "spectrophotometry",
        "chromatography", "crystallization", "rheology", "propulsion", "avionics",

        # Academic, Repository & Search Terms
        "definition", "explanation", "methodology", "methodologies", "application",
        "applications", "difference", "differences", "comparison", "dissertation",
        "dissertations", "thesis", "theses", "research", "professor", "advisor",
        "department", "author", "summary", "overview", "experiment", "experimental",
        "investigation", "investigations", "analysis", "theoretical", "numerical",
        "simulation", "simulations", "performance", "validation", "evaluation",
        "publication", "publications", "doctoral", "doctorate", "undergraduate",
        "postgraduate", "scholar", "laboratory", "monograph", "proceedings",
    }

    def __init__(self):
        # 1. Load valid English words dictionary to protect common words (e.g. 'tell' -> never 'cell')
        self.valid_english_words: Set[str] = self._load_valid_english_words()

        # 2. Build target institutional vocabulary
        self.vocab: Set[str] = set(w.lower() for w in self.BASE_VOCABULARY)
        self._load_department_aliases()

        # 3. Pre-index targets into length-bounded candidate buckets
        self.length_buckets: Dict[int, List[str]] = defaultdict(list)
        for w in self.vocab:
            self.length_buckets[len(w)].append(w)

    def _load_valid_english_words(self) -> Set[str]:
        """Load standard system English word lists to protect non-misspelled vocabulary."""
        words = set(self.FALLBACK_VALID_WORDS)
        for path in ("/usr/share/dict/words", "/usr/dict/words"):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            w = line.strip().lower()
                            if w.isalpha():
                                words.add(w)
                except Exception:
                    pass
        return words

    def _load_department_aliases(self) -> None:
        """Dynamically add all words from QueryMetadataExtractor department aliases."""
        try:
            from pipeline.query_metadata_extractor import QueryMetadataExtractor
            for alias in QueryMetadataExtractor.DEPARTMENT_ALIASES.keys():
                for token in alias.lower().split():
                    if len(token) >= 4 and token.isalpha():
                        self.vocab.add(token)
        except Exception:
            pass

    @classmethod
    def get_instance(cls) -> "TypoNormalizer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _fast_levenshtein(s1: str, s2: str, max_dist: int = 2) -> int:
        """Fast row-swapping Levenshtein with early-exit boundary termination."""
        if s1 == s2:
            return 0
        l1, l2 = len(s1), len(s2)
        if abs(l1 - l2) > max_dist:
            return max_dist + 1
        if l1 < l2:
            s1, s2 = s2, s1
            l1, l2 = l2, l1

        prev = list(range(l2 + 1))
        for i, c1 in enumerate(s1):
            curr = [i + 1] * (l2 + 1)
            min_in_row = curr[0]
            for j, c2 in enumerate(s2):
                cost = 0 if c1 == c2 else 1
                curr[j + 1] = min(prev[j + 1] + 1, curr[j] + 1, prev[j] + cost)
                if curr[j + 1] < min_in_row:
                    min_in_row = curr[j + 1]
            if min_in_row > max_dist:
                return max_dist + 1
            prev = curr
        return prev[l2]

    def _correct_token_cached(self, token_lower: str) -> str:
        """Correct a single lowercased token. Decisions are cached only when the corpus could be checked; without
        the index (fresh clone, Redis down) the English dictionary alone protects words, uncached."""
        try:
            return self._correct_token_checked(token_lower)
        except CorpusUnavailable:
            return self._correct_token(token_lower, check_corpus=False)

    @functools.lru_cache(maxsize=32768)
    def _correct_token_checked(self, token_lower: str) -> str:
        return self._correct_token(token_lower, check_corpus=True)

    def _correct_token(self, token_lower: str, check_corpus: bool = True) -> str:
        """Correct a single lowercased token."""
        # 1. Skip if already valid institutional vocab, valid English word, too short, or contains digits
        if (
            token_lower in self.vocab
            or token_lower in self.valid_english_words
            or len(token_lower) < 4
            or any(c.isdigit() for c in token_lower)
        ):
            return token_lower
        # DEFECT-40: a word that is a real word is never rewritten into a different one. Real = in the English
        # dictionary bundled with symspellpy (82,765 words; no system word list needed) or present in at least
        # CORPUS_MIN_CHUNKS thesis chunks (graphane, graphyne, ...). Only words found in neither are treated as typos.
        if _is_english(token_lower) or (check_corpus and _in_corpus(token_lower)):
            return token_lower

        max_allowed = 1 if len(token_lower) <= 6 else 2

        # 2. Check length-bounded candidate buckets
        candidates: List[str] = []
        for length in range(len(token_lower) - max_allowed, len(token_lower) + max_allowed + 1):
            candidates.extend(self.length_buckets.get(length, []))

        s_chars = set(token_lower)
        best_cand = token_lower
        min_dist = max_allowed + 1

        # 3. Filter candidates by character overlap before computing edit distance
        for cand in candidates:
            if len(s_chars & set(cand)) < max(2, len(s_chars) - max_allowed):
                continue

            dist = self._fast_levenshtein(token_lower, cand, max_dist=max_allowed)
            if dist <= max_allowed and dist < min_dist:
                min_dist = dist
                best_cand = cand
                if dist == 1:
                    # Distance 1 is an immediate optimal match
                    break

        return best_cand

    def correct_word(self, word: str) -> str:
        """Correct a single word while preserving original casing."""
        if not word or len(word) < 4:
            return word

        token_lower = word.lower()
        corrected = self._correct_token_cached(token_lower)

        if corrected == token_lower:
            return word

        # Preserve original casing
        if word.isupper():
            return corrected.upper()
        if word[0].isupper():
            return corrected.capitalize()
        return corrected

    def normalize(self, query: str) -> str:
        """Normalize misspellings across all tokens in a natural language query."""
        if not query or len(query.strip()) < 4:
            return query

        def _repl(match: re.Match[str]) -> str:
            return self.correct_word(match.group(0))

        return re.sub(r"\b[A-Za-z0-9_-]+\b", _repl, query)
