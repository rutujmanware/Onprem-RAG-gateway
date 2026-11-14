#!/usr/bin/env python3
"""Single-file Chunk Retriever for Redis Vector Database.

Flow:
1. Receives a user query.
2. Embeds the query using the 384-dim dense embedder (all-MiniLM-L6-v2) on CPU.
3. Executes a KNN vector similarity search against the RediSearch index.
4. Retrieves and returns matching text chunks with metadata & similarity scores.
"""

from __future__ import annotations

import os
import sys

# CRITICAL: Prevent OpenMP / C++ threading conflicts
# These must be set before numpy, torch, or sentence_transformers are imported.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import argparse
import asyncio
import logging
import math
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

try:
    import redis
except ImportError:
    redis = None

from pipeline.embeddings.embedder import LocalEmbedder
from pipeline.query_metadata_extractor import QueryMetadataExtractor, ExtractedMetadata

logger = logging.getLogger("pipeline.retriever")

# Internal database keys and binary fields excluded from retriever output
INTERNAL_DB_KEYS = {
    "chunk_key",
    "handle_id",
    "chunk_index",
    "chunk_id",
    "vector",
    "embedding",
}


def strip_internal_keys(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """Strip internal database fields (chunk_key, vector, etc.) while preserving content for RAG reasoning."""
    if not isinstance(chunk, dict):
        return chunk
    return {k: v for k, v in chunk.items() if k not in INTERNAL_DB_KEYS}


def sanitize_chunk(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize chunk for API response: return only the document title (no raw chunk text from Redis)."""
    if not isinstance(chunk, dict):
        return chunk
    title = chunk.get("title") or chunk.get("doc_id") or "Untitled Document"
    res = {"title": title}
    if "similarity" in chunk:
        res["similarity"] = chunk["similarity"]
    return res


def sanitize_chunks(chunks: List[Any]) -> List[Dict[str, Any]]:
    """Sanitize a list of chunks for response by keeping only unique document titles."""
    if not isinstance(chunks, list):
        return chunks
    seen_titles = set()
    unique_chunks = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        sc = sanitize_chunk(c)
        title = (sc.get("title") or "").strip()
        if title:
            if title not in seen_titles:
                seen_titles.add(title)
                unique_chunks.append(sc)
        else:
            unique_chunks.append(sc)
    return unique_chunks


def load_env_file():
    """Auto-load variables from .env if present."""
    candidates = [
        Path(".env"),
        ROOT_DIR / ".env",
        Path(__file__).resolve().parent / ".env"
    ]
    for env_path in candidates:
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
            break


load_env_file()


class RedisChunkRetriever:
    """Embeds queries and retrieves relevant chunks from Redis using dynamic similarity thresholding."""

    DEFAULT_MIN_SIMILARITY: float = float(os.getenv("RETRIEVAL_MIN_SIMILARITY", "0.52"))
    DEFAULT_SCORE_MARGIN: float = float(os.getenv("RETRIEVAL_SCORE_MARGIN", "0.10"))
    DEFAULT_MAX_KNEE_DROP: float = float(os.getenv("RETRIEVAL_MAX_KNEE_DROP", "0.06"))
    DEFAULT_EF_RUNTIME: int = int(os.getenv("RETRIEVAL_EF_RUNTIME", "64"))
    DEFAULT_DYNAMIC: bool = os.getenv("RETRIEVAL_DYNAMIC", "true").lower() in ("true", "1", "yes")
    DEFAULT_METADATA_FILTER: bool = os.getenv("RETRIEVAL_METADATA_FILTER", "true").lower() in ("true", "1", "yes")

    def __init__(
        self,
        redis_url: Optional[str] = None,
        index_name: str = "rag_documents_idx",
        vector_dim: int = 384,
        device: str = "cpu",
        min_similarity: Optional[float] = None,
        score_margin: Optional[float] = None,
        max_knee_drop: Optional[float] = None,
        ef_runtime: Optional[int] = None,
        dynamic: Optional[bool] = None,
        enable_metadata_filter: Optional[bool] = None,
    ):
        _redis_pw = os.getenv("REDIS_PASSWORD", "")
        _pw_str = f":{_redis_pw}@" if _redis_pw else ""
        _default_redis_url = f"redis://{_pw_str}{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}/0"
        self.redis_url = redis_url or os.getenv("REDIS_URL") or _default_redis_url
        self.index_name = index_name or os.getenv("REDIS_INDEX_NAME", "rag_documents_idx")
        self.vector_dim = vector_dim
        # Force CPU device for embedding to avoid any GPU/CUDA context driver crashes
        self.embedder = LocalEmbedder(dimension=self.vector_dim, device=device)
        self.metadata_extractor = QueryMetadataExtractor()

        self.min_similarity = min_similarity if min_similarity is not None else self.DEFAULT_MIN_SIMILARITY
        self.score_margin = score_margin if score_margin is not None else self.DEFAULT_SCORE_MARGIN
        self.max_knee_drop = max_knee_drop if max_knee_drop is not None else self.DEFAULT_MAX_KNEE_DROP
        self.ef_runtime = ef_runtime if ef_runtime is not None else self.DEFAULT_EF_RUNTIME
        self.dynamic = dynamic if dynamic is not None else self.DEFAULT_DYNAMIC
        self.enable_metadata_filter = enable_metadata_filter if enable_metadata_filter is not None else self.DEFAULT_METADATA_FILTER

    # ------------------------------------------------------------------------------------------------
    # Keyword-assisted retrieval for short topic questions ("explain transformers", "what is X")
    #   A 1-3 word topic gives near-random dense similarities (0.49-0.63 against mostly unrelated
    #   chunks), so whether the 0.52 floor lets anything through depends on phrasing. For such
    #   questions the vector search is restricted to chunks whose title or text contains the exact
    #   topic word(s) (VERBATIM: no stemming, so 'transformer' does not match 'transformation'),
    #   title matches are ranked first, and a lower floor applies because the keyword is evidence.
    # ------------------------------------------------------------------------------------------------
    KEYWORD_MIN_SIMILARITY: float = float(os.getenv("RETRIEVAL_KEYWORD_MIN_SIMILARITY", "0.45"))
    # The lower floor applies only to TITLE matches of a SPECIFIC topic (few title chunks contain all the
    # topic words), e.g. 'transformer' (1,820) or 'gmres' (503), not 'life' (12,606) or 'energy' (21,190);
    # otherwise the normal floor applies, so out-of-domain questions ("meaning of life") are still refused.
    KEYWORD_SPECIFIC_MAX_TITLE_CHUNKS: int = int(os.getenv("RETRIEVAL_KEYWORD_SPECIFIC_MAX_TITLE_CHUNKS", "5000"))
    TOPIC_MAX_TERMS: int = 3
    _TOPIC_PREFIX = re.compile(
        r"^(?:(?:please|kindly)\s+|(?:can|could|would)\s+you\s+(?:please\s+)?)*"
        r"(?:explain|describe|define|summari[sz]e|elaborate\s+on|tell\s+me\s+about|tell\s+me|"
        r"give\s+(?:me\s+)?an?\s+overview\s+of|overview\s+of|introduction\s+to|meaning\s+of|"
        r"what\s+(?:is|are|was|were)|what's|who\s+(?:is|are))\s+(?:the\s+|a\s+|an\s+)?",
        re.IGNORECASE,
    )
    _TOPIC_STOP = {
        "the", "a", "an", "of", "in", "on", "for", "and", "to", "about", "me", "please", "briefly", "detail",
        "detailed", "simple", "terms", "concept", "topic", "is", "are", "what", "explain", "describe", "define",
        "meaning", "overview", "introduction", "basics", "basic", "with", "examples", "example", "simply",
        "working", "workings", "principle", "principles", "how", "does", "work", "works",
        # "What is known about X in the theses / IISc research?": framing words, not topic words. Every topic
        # term must appear in a thesis title, so one of these left in made the question match nothing.
        "known", "thesis", "theses", "dissertation", "dissertations", "research", "iisc",
    }

    @classmethod
    def topic_terms(cls, query: str) -> Optional[List[str]]:
        """Return the 1-3 content words of a short topic question, else None."""
        text = (query or "").lower().replace("\u2019", "'").strip().rstrip("?.! ")
        stripped = cls._TOPIC_PREFIX.sub("", text)
        # Hyphenated words are split: the index tokenizes 'lithium-ion' as 'lithium' + 'ion'.
        tokens = [t for t in re.findall(r"[a-z][a-z0-9]+", stripped) if t not in cls._TOPIC_STOP and len(t) >= 3]
        if not tokens or len(tokens) > cls.TOPIC_MAX_TERMS:
            return None
        if stripped == text and len(re.findall(r"[a-z0-9]+", text)) > cls.TOPIC_MAX_TERMS:
            return None  # a full question without an instruction prefix is not a topic query
        return tokens

    @staticmethod
    def _term_variants(term: str) -> List[str]:
        """Word, lemma and singular/plural forms via lemminflect (no stemming: 'transformation' stays separate)."""
        from pipeline.text_processing.topic_speller import TopicSpeller
        return TopicSpeller.get_instance().variants(re.sub(r"[^a-z0-9]", "", term.lower()))

    def _title_all_terms_query(self, terms: List[str]) -> str:
        return " ".join(f"@title:({'|'.join(self._term_variants(t))})" for t in terms)

    def _is_specific(self, title_chunk_count: int) -> bool:
        return 0 < title_chunk_count <= self.KEYWORD_SPECIFIC_MAX_TITLE_CHUNKS

    @staticmethod
    def _total(results: Any) -> int:
        if isinstance(results, dict):
            return int(results.get(b"total_results", results.get("total_results", 0)) or 0)
        return int(results[0]) if isinstance(results, (list, tuple)) and results else 0

    @staticmethod
    def _apply_corrections(text: str, corrections: Dict[str, str]) -> str:
        """Embed the user's own question with misspelled topic words replaced (not the bare word)."""
        for wrong, right in corrections.items():
            text = re.sub(rf"\b{re.escape(wrong)}\b", right, text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def _merge_hits(*hit_lists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen, merged = set(), []
        for hits in hit_lists:
            for h in hits:
                key = (h.get("doc_id"), h.get("chunk_index"), str(h.get("content", ""))[:80])
                if key not in seen:
                    seen.add(key)
                    merged.append(h)
        return merged

    def _keyword_filter(self, terms: List[str]) -> str:
        clauses = []
        for t in terms:
            alts = "|".join(self._term_variants(t))
            clauses.append(f"(@title:({alts}) | @content:({alts}))")
        return " ".join(clauses)

    def _rank_keyword_hits(self, hits: List[Dict[str, Any]], terms: List[str], max_k: int,
                           specific: bool = False) -> List[Dict[str, Any]]:
        words = {v for t in terms for v in self._term_variants(t)}
        title_floor = self.KEYWORD_MIN_SIMILARITY if specific else self.min_similarity
        kept = []
        for c in hits:
            sim = float(c.get("similarity", 0))
            title = str(c.get("title") or "").lower()
            title_match = any(re.search(rf"\b{re.escape(w)}\b", title) for w in words)
            if sim < (title_floor if title_match else self.min_similarity):
                continue
            c["keyword_title_match"] = title_match
            c["retrieval_mode"] = "keyword_assisted"
            kept.append(c)
        kept.sort(key=lambda c: (c["keyword_title_match"], float(c.get("similarity", 0))), reverse=True)
        return kept[:max_k]

    def _execute_keyword_knn(self, r: Any, text: str, terms: List[str], k: int):
        """Returns (hits, specific, resolved_terms, corrections)."""
        k = min(max(1, int(k)), 50)
        from pipeline.text_processing.topic_speller import TopicSpeller
        terms, corrections = TopicSpeller.get_instance().correct_terms(terms)
        vec_bytes = np.array(self.embedder.embed_query(self._apply_corrections(text, corrections)), dtype=np.float32).tobytes()
        knn = f"=>[KNN {k} @vector $vec EF_RUNTIME {self.ef_runtime} AS score]"
        hit_lists = []
        for flt in (self._title_all_terms_query(terms), self._keyword_filter(terms)):  # title matches get their own search
            res = r.execute_command("FT.SEARCH", self.index_name, f"({flt}){knn}", "VERBATIM",
                                    "PARAMS", "2", "vec", vec_bytes, "SORTBY", "score", "ASC", "DIALECT", "2")
            hit_lists.append(self._parse_results(res))
        count = r.execute_command("FT.SEARCH", self.index_name, self._title_all_terms_query(terms), "VERBATIM",
                                  "LIMIT", "0", "0", "DIALECT", "2")
        return self._merge_hits(*hit_lists), self._is_specific(self._total(count)), terms, corrections

    async def _aexecute_keyword_knn(self, r: Any, text: str, terms: List[str], k: int):
        k = min(max(1, int(k)), 50)
        from pipeline.text_processing.topic_speller import TopicSpeller
        terms, corrections = TopicSpeller.get_instance().correct_terms(terms)
        loop = asyncio.get_running_loop()
        vec = await loop.run_in_executor(None, self.embedder.embed_query, self._apply_corrections(text, corrections))
        vec_bytes = np.array(vec, dtype=np.float32).tobytes()
        knn = f"=>[KNN {k} @vector $vec EF_RUNTIME {self.ef_runtime} AS score]"
        hit_lists = []
        for flt in (self._title_all_terms_query(terms), self._keyword_filter(terms)):
            res = await r.execute_command("FT.SEARCH", self.index_name, f"({flt}){knn}", "VERBATIM",
                                          "PARAMS", "2", "vec", vec_bytes, "SORTBY", "score", "ASC", "DIALECT", "2")
            hit_lists.append(self._parse_results(res))
        count = await r.execute_command("FT.SEARCH", self.index_name, self._title_all_terms_query(terms), "VERBATIM",
                                        "LIMIT", "0", "0", "DIALECT", "2")
        return self._merge_hits(*hit_lists), self._is_specific(self._total(count)), terms, corrections

    # ------------------------------------------------------------------------------------------------
    # Rare query words ("What is known about GMRES?", "How does GMRES compare with MINRES?")
    #   The embedding model does not know rare technical terms, so a question built around one scores
    #   only ~0.35-0.45 against the very chunks that discuss it, below the 0.52 floor. A rare word the
    #   user typed is strong evidence by itself: chunks containing it (title or text, VERBATIM + lemmas)
    #   are fetched with a filtered vector search, accepted from a lower floor (RARE_MIN_SIMILARITY) and
    #   ranked with a bonus. Only words in <= RARE_MAX_CHUNKS of the 2.26 M chunks count as rare; common
    #   English words are skipped without a query. The global floor and all other paths are unchanged.
    # ------------------------------------------------------------------------------------------------
    RARE_TERMS_ENABLED: bool = os.getenv("RETRIEVAL_RARE_TERMS", "true").lower() in ("true", "1", "yes")
    RARE_MAX_CHUNKS: int = int(os.getenv("RETRIEVAL_RARE_MAX_CHUNKS", "2000"))
    RARE_MIN_SIMILARITY: float = float(os.getenv("RETRIEVAL_RARE_MIN_SIMILARITY", "0.30"))
    RARE_SCORE_BONUS: float = float(os.getenv("RETRIEVAL_RARE_SCORE_BONUS", "0.15"))
    RARE_TITLE_BONUS: float = 0.05
    RARE_MAX_ADDED: int = int(os.getenv("RETRIEVAL_RARE_MAX_ADDED", "3"))
    RARE_MIN_DOC_CHUNKS: int = int(os.getenv("RETRIEVAL_RARE_MIN_DOC_CHUNKS", "2"))  # corroboration, unless title match
    RARE_MAX_TERMS: int = 2          # the rarest words of the question
    RARE_MAX_CANDIDATES: int = 6     # words per question checked against the index
    RARE_COMMON_ENGLISH_FREQ: int = 1_000_000  # symspell count; above it a word is everyday English
    _RARE_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
    _RARE_DF_CACHE: Dict[str, int] = {}
    _ENGLISH_FREQ: Optional[Dict[str, int]] = None
    _ENGLISH_FREQ_LOCK = threading.Lock()

    @classmethod
    def _english_freq(cls, word: str) -> int:
        if cls._ENGLISH_FREQ is None:
            with cls._ENGLISH_FREQ_LOCK:
                if cls._ENGLISH_FREQ is None:
                    freq: Dict[str, int] = {}
                    try:
                        from importlib.resources import files
                        with open(str(files("symspellpy") / "frequency_dictionary_en_82_765.txt")) as fh:
                            for line in fh:
                                parts = line.split()
                                if len(parts) == 2:
                                    freq[parts[0]] = int(parts[1])
                    except Exception as e:  # no dictionary: rely on the index counts alone
                        logger.warning("English frequency dictionary unavailable: %s", e)
                    cls._ENGLISH_FREQ = freq
        return cls._ENGLISH_FREQ.get(word, 0)

    @classmethod
    def rare_candidates(cls, query: str) -> List[str]:
        """Content words of the question that could be rare (not stopwords, not everyday English)."""
        out: List[str] = []
        for w in cls._RARE_WORD.findall((query or "").lower()):
            if w in out or w in cls._TOPIC_STOP or cls._english_freq(w) >= cls.RARE_COMMON_ENGLISH_FREQ:
                continue
            out.append(w)
        return out[:cls.RARE_MAX_CANDIDATES]

    def _rare_df_query(self, word: str) -> Tuple[str, ...]:
        return ("FT.SEARCH", self.index_name, self._keyword_filter([word]), "VERBATIM", "LIMIT", "0", "0", "DIALECT", "2")

    def _remember_df(self, word: str, n: int) -> int:
        if len(self._RARE_DF_CACHE) > 50_000:
            self._RARE_DF_CACHE.clear()
        self._RARE_DF_CACHE[word] = n
        return n

    def _pick_rare(self, dfs: List[Tuple[str, int]]) -> List[str]:
        rare = sorted((d, w) for w, d in dfs if 0 < d <= self.RARE_MAX_CHUNKS)
        return [w for _, w in rare[:self.RARE_MAX_TERMS]]

    def _rare_knn_query(self, word: str) -> str:
        return f"({self._keyword_filter([word])})=>[KNN 10 @vector $vec EF_RUNTIME {self.ef_runtime} AS score]"

    def _select_rare_hits(self, hits: List[Tuple[str, List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
        kept, seen = [], set()
        for word, chunks in hits:
            variants = self._term_variants(word)
            ok = [c for c in chunks if float(c.get("similarity", 0.0)) >= self.RARE_MIN_SIMILARITY]
            per_doc: Dict[Any, int] = {}
            for c in ok:
                per_doc[c.get("doc_id")] = per_doc.get(c.get("doc_id"), 0) + 1
            for c in ok:
                key = (c.get("doc_id"), c.get("chunk_index"), str(c.get("content", ""))[:80])
                if key in seen:
                    continue
                title = str(c.get("title") or "").lower()
                title_match = any(re.search(rf"\b{re.escape(v)}\b", title) for v in variants)
                # Corroboration (as the dense path's coherence guard): a thesis that discusses the word
                # mentions it in several chunks; a lone mention is usually a citation or an aside
                # ("Messi" as a cited author in a chemistry thesis).
                if not title_match and per_doc[c.get("doc_id")] < self.RARE_MIN_DOC_CHUNKS:
                    continue
                seen.add(key)
                c["keyword_title_match"] = title_match
                c["rare_term"] = word
                c["retrieval_mode"] = "rare_term"
                kept.append(c)
        kept.sort(key=self._rare_rank_score, reverse=True)
        return kept[:self.RARE_MAX_ADDED]

    def _rare_rank_score(self, c: Dict[str, Any]) -> float:
        return float(c.get("similarity", 0.0)) + self.RARE_SCORE_BONUS + (self.RARE_TITLE_BONUS if c.get("keyword_title_match") else 0.0)

    def _merge_rare(self, rare: List[Dict[str, Any]], dense: List[Dict[str, Any]],
                    top_k: Optional[int]) -> List[Dict[str, Any]]:
        """Rare-word chunks ranked with their bonus among the normal results (duplicates dropped)."""
        if not rare:
            return dense
        key = lambda c: (c.get("doc_id"), c.get("chunk_index"), str(c.get("content", ""))[:80])
        rare_keys = {key(c) for c in rare}
        scored = [(self._rare_rank_score(c), c) for c in rare]
        scored += [(float(c.get("similarity", 0.0)), c) for c in dense if key(c) not in rare_keys]
        scored.sort(key=lambda x: x[0], reverse=True)
        merged = [c for _, c in scored]
        return merged[:top_k] if top_k else merged

    def _rare_term_search(self, r: Any, query: str, vec_bytes: bytes) -> List[Dict[str, Any]]:
        dfs = []
        for w in self.rare_candidates(query):
            n = self._RARE_DF_CACHE.get(w)
            if n is None:
                n = self._remember_df(w, self._total(r.execute_command(*self._rare_df_query(w))))
            dfs.append((w, n))
        hits = []
        for w in self._pick_rare(dfs):
            res = r.execute_command("FT.SEARCH", self.index_name, self._rare_knn_query(w), "VERBATIM",
                                    "PARAMS", "2", "vec", vec_bytes, "SORTBY", "score", "ASC", "DIALECT", "2")
            hits.append((w, self._parse_results(res)))
        return self._select_rare_hits(hits)

    async def _arare_term_search(self, r: Any, query: str, vec_bytes: bytes) -> List[Dict[str, Any]]:
        dfs = []
        for w in self.rare_candidates(query):
            n = self._RARE_DF_CACHE.get(w)
            if n is None:
                n = self._remember_df(w, self._total(await r.execute_command(*self._rare_df_query(w))))
            dfs.append((w, n))
        hits = []
        for w in self._pick_rare(dfs):
            res = await r.execute_command("FT.SEARCH", self.index_name, self._rare_knn_query(w), "VERBATIM",
                                          "PARAMS", "2", "vec", vec_bytes, "SORTBY", "score", "ASC", "DIALECT", "2")
            hits.append((w, self._parse_results(res)))
        return self._select_rare_hits(hits)

    def _build_filter_expression(self, filter_dict: Optional[Dict[str, Any]] = None) -> str:
        """Format optional metadata filter dictionary into RediSearch query clauses.

        Sanitizes punctuation and uses tokenized search so '@department:(Materials Engineering)'
        matches DSpace fields like 'Materials Engineering (MatEng) / View Item'.
        """
        if not filter_dict:
            return "*"
        clauses = []
        for field, value in filter_dict.items():
            if value is None or value == "":
                continue
            if isinstance(value, (int, float)):
                clauses.append(f"@{field}:[{value} {value}]")
            else:
                raw_val = str(value).strip()
                # Clean punctuation to prevent RediSearch query syntax errors
                clean_terms = re.findall(r'[A-Za-z0-9_]+', raw_val)
                if clean_terms:
                    terms_str = " ".join(clean_terms)
                    clauses.append(f'@{field}:({terms_str})')
        return " ".join(clauses) if clauses else "*"

    def _build_catalog_query(self, filter_dict: Optional[Dict[str, Any]], clean_query: str) -> Tuple[str, Optional[str]]:
        """Construct RediSearch clauses for catalog/existence/count queries."""
        clauses = []
        fallback_query = None
        filter_dict = filter_dict or {}

        # 1. Department clause
        if "department" in filter_dict and filter_dict["department"]:
            dept_terms = " ".join(re.findall(r'[A-Za-z0-9]+', str(filter_dict["department"])))
            if dept_terms:
                clauses.append(f"@department:({dept_terms})")

        # 2. Author or Advisor clause (searches author, advisor, and thesis content/committee)
        person_name = filter_dict.get("advisor") or filter_dict.get("author")
        if person_name:
            person_terms = " ".join(re.findall(r'[A-Za-z0-9]+', str(person_name)))
            if person_terms:
                clauses.append(f"((@author:({person_terms})) | (@advisor:({person_terms})) | (@content:({person_terms})))")
                tokens = person_terms.split()
                if len(tokens) > 1:
                    last_tok = tokens[-1]
                    fallback_clauses = list(clauses[:-1])
                    fallback_clauses.append(f"((@author:({last_tok})) | (@advisor:({last_tok})) | (@content:({last_tok})))")
                    fallback_query = " ".join(fallback_clauses)

        # 3. Year / Date clause
        if "date" in filter_dict and filter_dict["date"]:
            date_terms = " ".join(re.findall(r'[0-9]+', str(filter_dict["date"])))
            if date_terms:
                clauses.append(f"@date:({date_terms})")

        # 4. Degree clause
        if "degree" in filter_dict and filter_dict["degree"]:
            deg_terms = " ".join(re.findall(r'[A-Za-z0-9]+', str(filter_dict["degree"])))
            if deg_terms:
                clauses.append(f"@degree:({deg_terms})")

        # 5. Handle ID clause
        if "handle_id" in filter_dict and filter_dict["handle_id"]:
            handle_terms = " ".join(re.findall(r'[0-9]+', str(filter_dict["handle_id"])))
            if handle_terms:
                clauses.append(f"@handle_id:({handle_terms})")

        # 6. Title clause
        if "title" in filter_dict and filter_dict["title"]:
            title_terms = " ".join(re.findall(r'[A-Za-z0-9]+', str(filter_dict["title"])))
            if title_terms:
                clauses.append(f"@title:({title_terms})")

        # 7. Topic / Clean Query clause
        if clean_query and clean_query.strip():
            topic_terms = " ".join(re.findall(r'[A-Za-z0-9]+', clean_query.strip()))
            if topic_terms:
                clauses.append(f"((@title:({topic_terms})) | (@content:({topic_terms})))")
                if fallback_query:
                    fallback_query += f" ((@title:({topic_terms})) | (@content:({topic_terms})))"

        full_query = " ".join(clauses) if clauses else "*"
        return full_query, fallback_query

    def _execute_catalog_search(
        self,
        r: Any,
        raw_query: str,
        filter_dict: Optional[Dict[str, Any]] = None,
        clean_query: str = "",
        is_count: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Execute exact RediSearch catalog query grouped by doc_id."""
        catalog_q, fallback_q = self._build_catalog_query(filter_dict, clean_query)
        results = r.execute_command(
            "FT.SEARCH",
            self.index_name,
            catalog_q,
            "RETURN", "8", "doc_id", "title", "author", "department", "advisor", "date", "handle_id", "content",
            "LIMIT", "0", str(limit),
            "DIALECT", "2",
        )

        total_chunks = results.get(b"total_results", results.get("total_results", 0)) if isinstance(results, dict) else 0
        raw_items = results.get(b"results", results.get("results", [])) if isinstance(results, dict) else []

        if total_chunks == 0 and fallback_q:
            results_fb = r.execute_command(
                "FT.SEARCH",
                self.index_name,
                fallback_q,
                "RETURN", "8", "doc_id", "title", "author", "department", "advisor", "date", "handle_id", "content",
                "LIMIT", "0", str(limit),
                "DIALECT", "2",
            )
            fb_total = results_fb.get(b"total_results", results_fb.get("total_results", 0)) if isinstance(results_fb, dict) else 0
            if fb_total > 0:
                results = results_fb
                total_chunks = fb_total
                raw_items = results.get(b"results", results.get("results", [])) if isinstance(results, dict) else []

        if total_chunks == 0 or not raw_items:
            return []

        theses: Dict[str, Dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id") or item.get(b"id") or ""
            chunk_key = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
            attrs = item.get("extra_attributes") or item.get(b"extra_attributes") or item
            item_data: Dict[str, str] = {}
            if isinstance(attrs, dict):
                for k, v in attrs.items():
                    str_k = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                    str_v = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v)
                    item_data[str_k] = str_v

            did = item_data.get("doc_id") or chunk_key
            if did not in theses:
                theses[did] = {
                    "doc_id": did,
                    "title": item_data.get("title", "Unknown Title"),
                    "author": item_data.get("author", "Unknown Author"),
                    "department": item_data.get("department", "Unknown Department"),
                    "advisor": item_data.get("advisor", ""),
                    "date": item_data.get("date", ""),
                    "handle_id": item_data.get("handle_id", ""),
                    "content_snippet": item_data.get("content", "")[:350],
                }

        num_theses = len(theses)
        summary_lines = [
            "INSTITUTIONAL REPOSITORY CATALOG RESULTS:",
        ]
        target_person = filter_dict.get("advisor") or filter_dict.get("author") if filter_dict else None
        target_dept = filter_dict.get("department") if filter_dict else None
        if target_dept:
            summary_lines.append(f"According to the official institutional repository catalog, exactly {num_theses} thesis/paper records exist in the {target_dept} department:")
        elif target_person:
            summary_lines.append(f"According to the official institutional repository catalog, exactly {num_theses} paper/thesis records exist authored or supervised by {target_person}:")
        elif clean_query and clean_query.strip():
            summary_lines.append(f"According to the official institutional repository catalog, exactly {num_theses} papers/theses exist related to '{clean_query.strip()}':")
        else:
            summary_lines.append(f"According to the official institutional repository catalog, exactly {num_theses} matching records exist:")

        for idx, t in enumerate(theses.values(), 1):
            adv = t['advisor']
            if not adv and filter_dict and filter_dict.get("advisor"):
                adv = filter_dict["advisor"]
            adv_str = f" | Thesis Advisor: {adv}" if adv else ""
            year_str = f" | Date: {t['date']}" if t['date'] else ""
            handle_str = f" | Handle: {t['handle_id']}" if t['handle_id'] else ""
            summary_lines.append(
                f"{idx}. Title: {t['title']} | Author: {t['author']} | Department: {t['department']}{adv_str}{year_str}{handle_str}"
            )

        catalog_title = f"Official Repository Catalog - {target_dept or target_person or 'Catalog Index'}"
        display_summary = summary_lines[:40]
        if len(summary_lines) > 40:
            display_summary.append(f"... and {len(summary_lines) - 40} additional thesis/paper records in this department.")
        summary_chunk = {
            "doc_id": "catalog_summary",
            "title": catalog_title,
            "author": "IISc Repository Catalog",
            "department": filter_dict.get("department", "IISc Institutional Repository") if filter_dict else "IISc Institutional Repository",
            "similarity": 1.0,
            "content": "\n".join(display_summary),
        }

        chunks = [summary_chunk]
        for idx, t in enumerate(theses.values(), 1):
            author_str = t["author"]
            if not t["advisor"] and filter_dict and filter_dict.get("advisor"):
                author_str += f" (Supervised by {filter_dict['advisor']})"
            chunks.append({
                "doc_id": t["doc_id"],
                "title": t["title"],
                "author": author_str,
                "department": t["department"],
                "advisor": t["advisor"],
                "date": t["date"],
                "handle_id": t["handle_id"],
                "similarity": round(max(0.70, 0.99 - idx * 0.005), 3),
                "content": t["content_snippet"],
            })

        return chunks

    async def _aexecute_catalog_search(
        self,
        r: Any,
        raw_query: str,
        filter_dict: Optional[Dict[str, Any]] = None,
        clean_query: str = "",
        is_count: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Asynchronously execute exact RediSearch catalog query grouped by doc_id."""
        catalog_q, fallback_q = self._build_catalog_query(filter_dict, clean_query)
        results = await r.execute_command(
            "FT.SEARCH",
            self.index_name,
            catalog_q,
            "RETURN", "8", "doc_id", "title", "author", "department", "advisor", "date", "handle_id", "content",
            "LIMIT", "0", str(limit),
            "DIALECT", "2",
        )

        total_chunks = results.get(b"total_results", results.get("total_results", 0)) if isinstance(results, dict) else 0
        raw_items = results.get(b"results", results.get("results", [])) if isinstance(results, dict) else []

        if total_chunks == 0 and fallback_q:
            results_fb = await r.execute_command(
                "FT.SEARCH",
                self.index_name,
                fallback_q,
                "RETURN", "8", "doc_id", "title", "author", "department", "advisor", "date", "handle_id", "content",
                "LIMIT", "0", str(limit),
                "DIALECT", "2",
            )
            fb_total = results_fb.get(b"total_results", results_fb.get("total_results", 0)) if isinstance(results_fb, dict) else 0
            if fb_total > 0:
                results = results_fb
                total_chunks = fb_total
                raw_items = results.get(b"results", results.get("results", [])) if isinstance(results, dict) else []

        if total_chunks == 0 or not raw_items:
            return []

        theses: Dict[str, Dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id") or item.get(b"id") or ""
            chunk_key = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
            attrs = item.get("extra_attributes") or item.get(b"extra_attributes") or item
            item_data: Dict[str, str] = {}
            if isinstance(attrs, dict):
                for k, v in attrs.items():
                    str_k = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                    str_v = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v)
                    item_data[str_k] = str_v

            did = item_data.get("doc_id") or chunk_key
            if did not in theses:
                theses[did] = {
                    "doc_id": did,
                    "title": item_data.get("title", "Unknown Title"),
                    "author": item_data.get("author", "Unknown Author"),
                    "department": item_data.get("department", "Unknown Department"),
                    "advisor": item_data.get("advisor", ""),
                    "date": item_data.get("date", ""),
                    "handle_id": item_data.get("handle_id", ""),
                    "content_snippet": item_data.get("content", "")[:350],
                }

        num_theses = len(theses)
        summary_lines = [
            "INSTITUTIONAL REPOSITORY CATALOG RESULTS:",
        ]
        target_person = filter_dict.get("advisor") or filter_dict.get("author") if filter_dict else None
        target_dept = filter_dict.get("department") if filter_dict else None
        if target_dept:
            summary_lines.append(f"According to the official institutional repository catalog, exactly {num_theses} thesis/paper records exist in the {target_dept} department:")
        elif target_person:
            summary_lines.append(f"According to the official institutional repository catalog, exactly {num_theses} paper/thesis records exist authored or supervised by {target_person}:")
        elif clean_query and clean_query.strip():
            summary_lines.append(f"According to the official institutional repository catalog, exactly {num_theses} papers/theses exist related to '{clean_query.strip()}':")
        else:
            summary_lines.append(f"According to the official institutional repository catalog, exactly {num_theses} matching records exist:")

        for idx, t in enumerate(theses.values(), 1):
            adv = t['advisor']
            if not adv and filter_dict and filter_dict.get("advisor"):
                adv = filter_dict["advisor"]
            adv_str = f" | Thesis Advisor: {adv}" if adv else ""
            year_str = f" | Date: {t['date']}" if t['date'] else ""
            handle_str = f" | Handle: {t['handle_id']}" if t['handle_id'] else ""
            summary_lines.append(
                f"{idx}. Title: {t['title']} | Author: {t['author']} | Department: {t['department']}{adv_str}{year_str}{handle_str}"
            )

        catalog_title = f"Official Repository Catalog - {target_dept or target_person or 'Catalog Index'}"
        display_summary = summary_lines[:40]
        if len(summary_lines) > 40:
            display_summary.append(f"... and {len(summary_lines) - 40} additional thesis/paper records in this department.")
        summary_chunk = {
            "doc_id": "catalog_summary",
            "title": catalog_title,
            "author": "IISc Repository Catalog",
            "department": filter_dict.get("department", "IISc Institutional Repository") if filter_dict else "IISc Institutional Repository",
            "similarity": 1.0,
            "content": "\n".join(display_summary),
        }

        chunks = [summary_chunk]
        for idx, t in enumerate(theses.values(), 1):
            author_str = t["author"]
            if not t["advisor"] and filter_dict and filter_dict.get("advisor"):
                author_str += f" (Supervised by {filter_dict['advisor']})"
            chunks.append({
                "doc_id": t["doc_id"],
                "title": t["title"],
                "author": author_str,
                "department": t["department"],
                "advisor": t["advisor"],
                "date": t["date"],
                "handle_id": t["handle_id"],
                "similarity": round(max(0.70, 0.99 - idx * 0.005), 3),
                "content": t["content_snippet"],
            })

        return chunks

    @classmethod
    def filter_dynamic_chunks(
        cls,
        chunks: List[Dict[str, Any]],
        min_similarity: Optional[float] = None,
        score_margin: Optional[float] = None,
        max_knee_drop: Optional[float] = None,
        max_k: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Filter raw candidate chunks dynamically based on similarity score distribution.

        1. Absolute Relevance Floor (min_similarity, default 0.48):
           Filters out irrelevant distractor noise and out-of-domain queries.
        2. Adaptive Relative Margin (score_margin, default 0.10):
           Keeps subsequent chunks only if their similarity is within score_margin of the top match.
           If the top match is borderline (< 0.52), margin tightens to 0.05 to prevent noise drift.
        3. Knee / Elbow Detection (max_knee_drop, default 0.06):
           Detects steep drops between consecutive ranks and truncates the tail.
        4. Upper Bound Clamp (max_k):
           Limits maximum number of returned chunks (e.g. 3 for CPU SLM, 8 for GPU LLM).
        """
        if not chunks:
            return []

        min_sim = min_similarity if min_similarity is not None else cls.DEFAULT_MIN_SIMILARITY
        margin = score_margin if score_margin is not None else cls.DEFAULT_SCORE_MARGIN
        knee_drop = max_knee_drop if max_knee_drop is not None else cls.DEFAULT_MAX_KNEE_DROP

        # 1. Filter candidates below absolute minimum similarity floor
        valid = [c for c in chunks if float(c.get("similarity", 0.0)) >= min_sim]
        if not valid:
            return []

        # Ensure sorted descending by similarity
        valid.sort(key=lambda x: float(x.get("similarity", 0.0)), reverse=True)

        top_sim = float(valid[0].get("similarity", 0.0))

        # Coherence Guard: If top match is borderline (< 0.55), require corroboration.
        # An isolated single chunk with no closely matching second chunk (within 0.04)
        # represents an accidental semantic false positive (e.g. tangential keyword match in an unrelated thesis).
        if top_sim < 0.55:
            if len(valid) < 2 or (top_sim - float(valid[1].get("similarity", 0.0))) > 0.04:
                return []

        # If top match is borderline (< 0.55), tighten margin to avoid noise drift
        effective_margin = margin if top_sim >= 0.55 else min(margin, 0.05)

        selected = [valid[0]]
        limit = max_k if (max_k is not None and max_k > 0) else len(valid)

        for i in range(1, min(len(valid), limit)):
            cur = valid[i]
            sim = float(cur.get("similarity", 0.0))
            prev_sim = float(valid[i - 1].get("similarity", 0.0))

            # Stop if drop from top match exceeds effective margin
            if (top_sim - sim) > effective_margin:
                break

            # Stop if consecutive drop exceeds knee threshold
            if (prev_sim - sim) > knee_drop:
                break

            selected.append(cur)

        return selected

    def _execute_knn_search(
        self,
        r: Any,
        text: str,
        k: int,
        filter_dict: Optional[Dict[str, Any]] = None,
        vec_bytes: Optional[bytes] = None,
    ) -> List[Dict[str, Any]]:
        """Run single KNN RediSearch vector query with optional metadata filter."""
        # VULN-06: Defensive bound clamp on vector search k
        k = min(max(1, int(k)), 50)
        if vec_bytes is None:
            vec_bytes = np.array(self.embedder.embed_query(text), dtype=np.float32).tobytes()
        base_filter = self._build_filter_expression(filter_dict)
        search_query = f"({base_filter})=>[KNN {k} @vector $vec EF_RUNTIME {self.ef_runtime} AS score]"
        results = r.execute_command(
            "FT.SEARCH",
            self.index_name,
            search_query,
            "PARAMS", "2", "vec", vec_bytes,
            "SORTBY", "score", "ASC",
            "DIALECT", "2",
        )
        return self._parse_results(results)

    async def _aexecute_knn_search(
        self,
        r: Any,
        text: str,
        k: int,
        filter_dict: Optional[Dict[str, Any]] = None,
        vec_bytes: Optional[bytes] = None,
    ) -> List[Dict[str, Any]]:
        """Asynchronous single KNN RediSearch vector query with optional metadata filter."""
        # VULN-06: Defensive bound clamp on vector search k
        k = min(max(1, int(k)), 50)
        if vec_bytes is None:
            loop = asyncio.get_running_loop()
            query_vec = await loop.run_in_executor(None, self.embedder.embed_query, text)
            vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()
        base_filter = self._build_filter_expression(filter_dict)
        search_query = f"({base_filter})=>[KNN {k} @vector $vec EF_RUNTIME {self.ef_runtime} AS score]"
        results = await r.execute_command(
            "FT.SEARCH",
            self.index_name,
            search_query,
            "PARAMS", "2", "vec", vec_bytes,
            "SORTBY", "score", "ASC",
            "DIALECT", "2",
        )
        return self._parse_results(results)

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter_dict: Optional[Dict[str, Any]] = None,
        dynamic: Optional[bool] = None,
        min_similarity: Optional[float] = None,
        score_margin: Optional[float] = None,
        max_knee_drop: Optional[float] = None,
        enable_metadata_filter: Optional[bool] = None,
        include_internal_ids: bool = False,
    ) -> List[Dict[str, Any]]:
        """Synchronous retrieval with automated query metadata extraction & hybrid fallback."""
        if not redis:
            raise RuntimeError("The 'redis' package is not installed. Run: pip install redis")

        from pipeline.text_processing.typo_normalizer import TypoNormalizer
        query = TypoNormalizer.get_instance().normalize(query)

        if top_k is not None:
            top_k = min(max(1, int(top_k)), 50)
        use_dynamic = self.dynamic if dynamic is None else dynamic
        fetch_k = max(top_k or 0, 30) if use_dynamic else (top_k or 10)
        use_meta = self.enable_metadata_filter if enable_metadata_filter is None else enable_metadata_filter

        # 1. Automatic Query Metadata Extraction
        extracted_meta: Optional[ExtractedMetadata] = None
        effective_filter = filter_dict
        search_text = query

        if use_meta and filter_dict is None:
            extracted_meta = self.metadata_extractor.extract(query)
            if extracted_meta.has_filters or extracted_meta.is_catalog_query:
                effective_filter = extracted_meta.filters
                search_text = extracted_meta.clean_query

        # 2. Stage 1: Execute search (Catalog search if catalog/count query, else KNN vector search)
        r = redis.from_url(self.redis_url, encoding="utf-8", decode_responses=False)
        fallback_triggered = False
        raw_hits = []
        keyword_chunks: List[Dict[str, Any]] = []
        rare_chunks: List[Dict[str, Any]] = []
        is_catalog_path = bool(
            extracted_meta and (
                extracted_meta.is_catalog_query
                or extracted_meta.is_count_query
                or (extracted_meta.has_filters and not extracted_meta.clean_query)
            )
        )
        try:
            if is_catalog_path:
                raw_hits = self._execute_catalog_search(
                    r,
                    raw_query=query,
                    filter_dict=effective_filter,
                    clean_query=search_text,
                    is_count=extracted_meta.is_count_query if extracted_meta else False,
                )
                if not raw_hits and (search_text or query):
                    logger.info("Catalog search returned 0 results, falling back to vector KNN search.")
                    fallback_triggered = True
                    raw_hits = self._execute_knn_search(r, search_text or query, fetch_k, filter_dict=None)
            else:
                terms = self.topic_terms(query) if not effective_filter else None
                if terms:
                    kw_hits, specific, terms, corrections = self._execute_keyword_knn(r, query, terms, fetch_k)
                    keyword_chunks = self._rank_keyword_hits(kw_hits, terms, top_k or 5, specific)
                    for c in keyword_chunks:
                        if corrections:
                            c["keyword_corrections"] = corrections
                if not keyword_chunks:
                    use_rare = self.RARE_TERMS_ENABLED and not effective_filter
                    dense_vec = (np.array(self.embedder.embed_query(search_text or query), dtype=np.float32).tobytes()
                                 if use_rare else None)
                    raw_hits = self._execute_knn_search(r, search_text or query, fetch_k, effective_filter, vec_bytes=dense_vec)
                    if use_rare:
                        rare_chunks = self._rare_term_search(r, query, dense_vec)

                # 3. Stage 2: Hybrid Fallback Check
                # If filtered search produced 0 matches, fall back to unconstrained search on raw query!
                if not raw_hits and effective_filter:
                    logger.info(
                        "Hybrid fallback: Filtered search with %s yielded 0 results. Falling back to full corpus vector search.",
                        effective_filter
                    )
                    fallback_triggered = True
                    raw_hits = self._execute_knn_search(r, query, fetch_k, filter_dict=None)
        finally:
            r.close()

        if keyword_chunks:
            final_chunks = keyword_chunks
        elif is_catalog_path:
            final_chunks = raw_hits[:top_k] if (top_k and not use_dynamic) else raw_hits
        elif not use_dynamic:
            final_chunks = raw_hits[:top_k] if top_k else raw_hits
        else:
            eff_min_sim = min_similarity if min_similarity is not None else self.min_similarity
            eff_margin = score_margin if score_margin is not None else self.score_margin
            eff_knee = max_knee_drop if max_knee_drop is not None else self.max_knee_drop
            final_chunks = self.filter_dynamic_chunks(
                raw_hits,
                min_similarity=eff_min_sim,
                score_margin=eff_margin,
                max_knee_drop=eff_knee,
                max_k=top_k,
            )
        if rare_chunks:
            final_chunks = self._merge_rare(rare_chunks, final_chunks, top_k)

        # Annotate chunks with metadata tracking
        for c in final_chunks:
            if extracted_meta and extracted_meta.has_filters:
                c["metadata_filter_extracted"] = extracted_meta.filters
                c["metadata_filter_applied"] = not fallback_triggered
                c["metadata_fallback_used"] = fallback_triggered

        if not include_internal_ids:
            final_chunks = [strip_internal_keys(c) for c in final_chunks]

        return final_chunks

    async def asearch(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter_dict: Optional[Dict[str, Any]] = None,
        dynamic: Optional[bool] = None,
        min_similarity: Optional[float] = None,
        score_margin: Optional[float] = None,
        max_knee_drop: Optional[float] = None,
        enable_metadata_filter: Optional[bool] = None,
        include_internal_ids: bool = False,
    ) -> List[Dict[str, Any]]:
        """Asynchronous retrieval with automated query metadata extraction & hybrid fallback."""
        if not aioredis:
            raise RuntimeError("The 'redis' package is not installed. Run: pip install redis")

        from pipeline.text_processing.typo_normalizer import TypoNormalizer
        query = TypoNormalizer.get_instance().normalize(query)

        if top_k is not None:
            top_k = min(max(1, int(top_k)), 50)
        use_dynamic = self.dynamic if dynamic is None else dynamic
        fetch_k = max(top_k or 0, 30) if use_dynamic else (top_k or 10)
        use_meta = self.enable_metadata_filter if enable_metadata_filter is None else enable_metadata_filter

        extracted_meta: Optional[ExtractedMetadata] = None
        effective_filter = filter_dict
        search_text = query

        if use_meta and filter_dict is None:
            extracted_meta = self.metadata_extractor.extract(query)
            if extracted_meta.has_filters or extracted_meta.is_catalog_query:
                effective_filter = extracted_meta.filters
                search_text = extracted_meta.clean_query

        r = aioredis.from_url(self.redis_url, encoding="utf-8", decode_responses=False)
        fallback_triggered = False
        raw_hits = []
        keyword_chunks: List[Dict[str, Any]] = []
        rare_chunks: List[Dict[str, Any]] = []
        is_catalog_path = bool(
            extracted_meta and (
                extracted_meta.is_catalog_query
                or extracted_meta.is_count_query
                or (extracted_meta.has_filters and not extracted_meta.clean_query)
            )
        )
        try:
            if is_catalog_path:
                raw_hits = await self._aexecute_catalog_search(
                    r,
                    raw_query=query,
                    filter_dict=effective_filter,
                    clean_query=search_text,
                    is_count=extracted_meta.is_count_query if extracted_meta else False,
                )
                if not raw_hits and (search_text or query):
                    logger.info("Async catalog search returned 0 results, falling back to vector KNN search.")
                    fallback_triggered = True
                    raw_hits = await self._aexecute_knn_search(r, search_text or query, fetch_k, filter_dict=None)
            else:
                terms = self.topic_terms(query) if not effective_filter else None
                if terms:
                    kw_hits, specific, terms, corrections = await self._aexecute_keyword_knn(r, query, terms, fetch_k)
                    keyword_chunks = self._rank_keyword_hits(kw_hits, terms, top_k or 5, specific)
                    for c in keyword_chunks:
                        if corrections:
                            c["keyword_corrections"] = corrections
                if not keyword_chunks:
                    use_rare = self.RARE_TERMS_ENABLED and not effective_filter
                    dense_vec = None
                    if use_rare:
                        loop = asyncio.get_running_loop()
                        dense_vec = np.array(await loop.run_in_executor(None, self.embedder.embed_query, search_text or query),
                                             dtype=np.float32).tobytes()
                    raw_hits = await self._aexecute_knn_search(r, search_text or query, fetch_k, effective_filter, vec_bytes=dense_vec)
                    if use_rare:
                        rare_chunks = await self._arare_term_search(r, query, dense_vec)

                if not raw_hits and effective_filter:
                    logger.info(
                        "Async hybrid fallback: Filtered search with %s yielded 0 results. Falling back to full corpus vector search.",
                        effective_filter
                    )
                    fallback_triggered = True
                    raw_hits = await self._aexecute_knn_search(r, query, fetch_k, filter_dict=None)
        finally:
            await r.aclose()

        if keyword_chunks:
            final_chunks = keyword_chunks
        elif is_catalog_path:
            final_chunks = raw_hits[:top_k] if (top_k and not use_dynamic) else raw_hits
        elif not use_dynamic:
            final_chunks = raw_hits[:top_k] if top_k else raw_hits
        else:
            eff_min_sim = min_similarity if min_similarity is not None else self.min_similarity
            eff_margin = score_margin if score_margin is not None else self.score_margin
            eff_knee = max_knee_drop if max_knee_drop is not None else self.max_knee_drop
            final_chunks = self.filter_dynamic_chunks(
                raw_hits,
                min_similarity=eff_min_sim,
                score_margin=eff_margin,
                max_knee_drop=eff_knee,
                max_k=top_k,
            )
        if rare_chunks:
            final_chunks = self._merge_rare(rare_chunks, final_chunks, top_k)

        for c in final_chunks:
            if extracted_meta and extracted_meta.has_filters:
                c["metadata_filter_extracted"] = extracted_meta.filters
                c["metadata_filter_applied"] = not fallback_triggered
                c["metadata_fallback_used"] = fallback_triggered

        if not include_internal_ids:
            final_chunks = [strip_internal_keys(c) for c in final_chunks]

        return final_chunks

    def _parse_results(self, results: Any) -> List[Dict[str, Any]]:
        if not results:
            return []

        hits = []

        # Case 1: redis-py dictionary response with 'results' / b'results'
        if isinstance(results, dict):
            raw_items = results.get("results") or results.get(b"results") or []
            for item in raw_items:
                chunk_data = {}
                if isinstance(item, dict):
                    raw_id = item.get("id") or item.get(b"id") or ""
                    chunk_key = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
                    chunk_data["chunk_key"] = chunk_key

                    attrs = item.get("extra_attributes") or item.get(b"extra_attributes") or item
                    if isinstance(attrs, dict):
                        for k, v in attrs.items():
                            str_k = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                            if str_k in ("vector", "embedding"):
                                continue
                            str_v = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v)
                            chunk_data[str_k] = str_v

                try:
                    distance = float(chunk_data.get("score", 1.0))
                    if math.isnan(distance):
                        distance = 1.0
                except (ValueError, TypeError):
                    distance = 1.0

                similarity = max(0.0, 1.0 - distance)
                chunk_data["similarity"] = round(similarity, 4)
                hits.append(chunk_data)
            return hits

        # Case 2: redis-py Result object with .docs attribute
        if hasattr(results, "docs"):
            for doc in results.docs:
                chunk_data = {"chunk_key": getattr(doc, "id", "")}
                for attr, val in doc.__dict__.items():
                    if attr in ("id", "payload", "vector", "embedding"):
                        continue
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="ignore")
                    chunk_data[attr] = val

                score = getattr(doc, "score", None) or chunk_data.get("score", 1.0)
                try:
                    distance = float(score)
                except (ValueError, TypeError):
                    distance = 1.0
                chunk_data["similarity"] = round(max(0.0, 1.0 - distance), 4)
                hits.append(chunk_data)
            return hits

        # Case 3: Raw list/tuple from redis execute_command
        if isinstance(results, (list, tuple)):
            if len(results) == 0 or results[0] == 0:
                return []

            for i in range(1, len(results), 2):
                if i >= len(results):
                    break
                raw_id = results[i]
                chunk_key = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
                chunk_data = {"chunk_key": chunk_key}

                if i + 1 < len(results):
                    fields = results[i + 1]
                    # If redis-py returned fields parsed as a dictionary:
                    if isinstance(fields, dict):
                        for k, v in fields.items():
                            str_k = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                            if str_k in ("vector", "embedding"):
                                continue
                            str_v = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v)
                            chunk_data[str_k] = str_v
                    # If fields is an alternating list [k1, v1, k2, v2, ...]
                    elif isinstance(fields, (list, tuple)):
                        for j in range(0, len(fields), 2):
                            if j + 1 < len(fields):
                                k = fields[j].decode("utf-8") if isinstance(fields[j], bytes) else str(fields[j])
                                if k in ("vector", "embedding"):
                                    continue
                                v = fields[j + 1].decode("utf-8", errors="ignore") if isinstance(fields[j + 1], bytes) else str(fields[j + 1])
                                chunk_data[k] = v

                distance = float(chunk_data.get("score", 1.0))
                similarity = max(0.0, 1.0 - distance)
                chunk_data["similarity"] = round(similarity, 4)
                hits.append(chunk_data)

            return hits

        return []


def retrieve_chunks(
    query: str,
    top_k: Optional[int] = None,
    filter_dict: Optional[Dict[str, Any]] = None,
    redis_url: Optional[str] = None,
    dynamic: Optional[bool] = None,
    min_similarity: Optional[float] = None,
    score_margin: Optional[float] = None,
    max_knee_drop: Optional[float] = None,
    enable_metadata_filter: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Convenience helper function to retrieve chunks in one line."""
    retriever = RedisChunkRetriever(
        redis_url=redis_url,
        min_similarity=min_similarity,
        score_margin=score_margin,
        max_knee_drop=max_knee_drop,
        dynamic=dynamic,
        enable_metadata_filter=enable_metadata_filter,
    )
    return retriever.search(
        query,
        top_k=top_k,
        filter_dict=filter_dict,
        dynamic=dynamic,
        min_similarity=min_similarity,
        score_margin=score_margin,
        max_knee_drop=max_knee_drop,
        enable_metadata_filter=enable_metadata_filter,
    )


def main():
    parser = argparse.ArgumentParser(description="Test chunk retrieval against Redis Vector Database")
    parser.add_argument("query", nargs="?", default=None, help="The search query text")
    parser.add_argument("-k", "--top-k", type=int, default=None, help="Maximum number of chunks to retrieve (default: 8 dynamic, 3 static)")
    parser.add_argument("--min-sim", type=float, default=None, help="Minimum cosine similarity floor (default: 0.48)")
    parser.add_argument("--margin", type=float, default=None, help="Max score margin from top candidate (default: 0.10)")
    parser.add_argument("--knee-drop", type=float, default=None, help="Max drop between consecutive candidates (default: 0.06)")
    parser.add_argument("--static", action="store_true", help="Disable dynamic filtering and retrieve static top-k")
    parser.add_argument("--no-meta-filter", action="store_true", help="Disable automated query metadata extraction")
    parser.add_argument("--index", default=os.getenv("REDIS_INDEX_NAME", "rag_documents_idx"), help="RediSearch index name")
    parser.add_argument("--redis-url", default=None, help="Redis URL (defaults to REDIS_URL or host/port/pw config)")

    args = parser.parse_args()

    if not args.query:
        print("\n❌ Error: Please provide a query to test!")
        print("Usage:")
        print("  python retriever.py \"deep learning for semantic segmentation\"\n")
        sys.exit(1)

    use_dynamic = not args.static
    use_meta = not args.no_meta_filter
    print("=" * 70)
    print("🔍 DYNAMIC CHUNK RETRIEVER (WITH HYBRID METADATA FALLBACK)")
    print("=" * 70)
    print(f"📡 Redis URL   : {args.redis_url}")
    print(f"📑 Index       : {args.index}")
    print(f"⚙️  Strategy    : {'⚡ DYNAMIC SCORE-BASED' if use_dynamic else '🔒 STATIC TOP-K'}")
    print(f"🏷️  Meta Filter : {'✅ ENABLED (HYBRID FALLBACK)' if use_meta else '❌ DISABLED'}")
    if use_dynamic:
        print(f"🎯 Min Sim Floor: {args.min_sim or RedisChunkRetriever.DEFAULT_MIN_SIMILARITY} | Margin: {args.margin or RedisChunkRetriever.DEFAULT_SCORE_MARGIN} | Knee Drop: {args.knee_drop or RedisChunkRetriever.DEFAULT_MAX_KNEE_DROP}")
        print(f"📊 Upper Bound : {args.top_k or 'Uncapped (Pool: 12)'}")
    else:
        print(f"🎯 Static K    : {args.top_k or 3}")
    print(f"❓ Query       : \"{args.query}\"")
    print("-" * 70)

    try:
        retriever = RedisChunkRetriever(
            redis_url=args.redis_url,
            index_name=args.index,
            min_similarity=args.min_sim,
            score_margin=args.margin,
            max_knee_drop=args.knee_drop,
            dynamic=use_dynamic,
            enable_metadata_filter=use_meta,
        )

        if use_meta:
            extracted_meta = retriever.metadata_extractor.extract(args.query)
            if extracted_meta.has_filters:
                print(f"🏷️  Auto-Extracted Filters: {extracted_meta.filters}")
                print(f"🧹 Clean Semantic Query  : \"{extracted_meta.clean_query}\"")
                print("-" * 70)

        print("⚡ 1. Embedding query text (CPU)...")
        emb = retriever.embedder.embed_query(args.query)
        print(f"   ✓ Vector embedded (Dimensions: {len(emb)})")

        print("⚡ 2. Searching Redis Vector Database...")
        chunks = retriever.search(
            args.query,
            top_k=args.top_k,
            dynamic=use_dynamic,
            min_similarity=args.min_sim,
            score_margin=args.margin,
            max_knee_drop=args.knee_drop,
            enable_metadata_filter=use_meta,
        )

        if not chunks:
            print(f"\n⚠️  No relevant chunks passed similarity threshold for index '{args.index}'.")
            print("💡 The query appears to be out-of-domain or has insufficient semantic match.")
            return

        if chunks[0].get("metadata_fallback_used"):
            print(f"\n⚠️  [HYBRID FALLBACK USED] Metadata filter {chunks[0].get('metadata_filter_extracted')} returned 0 results. Successfully fell back to full corpus vector search!")
        elif chunks[0].get("metadata_filter_applied"):
            print(f"\n🎯 [METADATA FILTER APPLIED] Search constrained to: {chunks[0].get('metadata_filter_extracted')}")

        print(f"\n✅ Dynamically Retrieved {len(chunks)} chunks from Redis:\n")
        for i, chunk in enumerate(chunks, 1):
            sim_pct = chunk.get("similarity", 0.0) * 100
            print(f"[{i}] {chunk.get('chunk_key')}  |  Cosine Similarity: {sim_pct:.2f}%")
            if chunk.get("title"):
                print(f"    Title      : {chunk['title']}")
            if chunk.get("author"):
                print(f"    Author     : {chunk['author']}")
            if chunk.get("department"):
                print(f"    Department : {chunk['department']}")
            if chunk.get("advisor"):
                print(f"    Advisor    : {chunk['advisor']}")
            content_preview = chunk.get("content", "").replace("\n", " ")[:300]
            print(f"    Content    : \"{content_preview}...\"\n")

        print("=" * 70)

    except Exception as e:
        print(f"\n❌ Retrieval Error: {type(e).__name__}: {e}")
        if "connection refused" in str(e).lower():
            print("   Hint: Ensure Redis is running and reachable.")
        elif "no such index" in str(e).lower() or "unknown index" in str(e).lower():
            print(f"   Hint: Index '{args.index}' does not exist in Redis.")
        else:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
