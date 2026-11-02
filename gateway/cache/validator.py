"""Three-stage cache validation mechanism for L1 Semantic Cache.

Implements:
1. Volatile / Dynamic Information Gate (rejects / bypasses cache for changing info).
2. Numerical Consistency Checker (extracts and validates numerical values and units).
3. Dimension Opposition Checker (prevents incorrect hits for "time" vs "space" complexity).
4. Stage 2: Context and Metadata Validation (model tier, language, conversation context, scope).
5. Stage 3: Verification & Reranking (Cross-Encoder, SLM Judge, or Lexical-Semantic Verifier).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# 1. Volatile / Dynamic Information Gate
# ============================================================================

class VolatileQueryDetector:
    """
    Detects queries that ask for rapidly changing or real-time information,
    such as current stock prices, weather, live availability, or timestamps.
    These queries must avoid caching unless freshness is explicitly handled.
    """

    # Categories of volatile / dynamic queries
    _PATTERNS = {
        "stock_and_finance": re.compile(
            r"\b(?:stock\s+price[s]?|share\s+price[s]?|market\s+price[s]?|ticker|crypto(?:currency)?|"
            r"bitcoin|ethereum|trading\s+at|market\s+cap\s+(?:today|now)|exchange\s+rate\s+(?:today|now))\b",
            re.IGNORECASE,
        ),
        "weather_and_climate": re.compile(
            r"\b(?:weather(?:\s+forecast)?|temperature\s+(?:today|now|right\s+now|currently)|"
            r"is\s+it\s+raining|humidity\s+(?:today|now)|weather\s+in\s+[a-zA-Z\s]+)\b",
            re.IGNORECASE,
        ),
        "live_availability_and_inventory": re.compile(
            r"\b(?:live\s+availability|current\s+availability|in\s+stock(?:\s+now)?|"
            r"seat[s]?\s+available|seat\s+availability|flight\s+status|flight\s+delay|"
            r"server\s+load(?:\s+now|\s+currently)?|traffic\s+(?:now|right\s+now|currently)|"
            r"queue\s+status(?:\s+now)?)\b",
            re.IGNORECASE,
        ),
        "temporal_and_realtime": re.compile(
            r"\b(?:what\s+time\s+is\s+it|current\s+time|today'?s\s+date|date\s+today|"
            r"right\s+now|at\s+this\s+moment|breaking\s+news(?:\s+today)?|live\s+score[s]?|"
            r"latest\s+status\s+now)\b",
            re.IGNORECASE,
        ),
    }

    # Whitelist exceptions to avoid false positives (e.g. "time complexity", "time series")
    _SAFE_EXCEPTIONS = re.compile(
        r"\b(?:time\s+complexity|space\s+complexity|time\s+series|execution\s+time|"
        r"convergence\s+time|training\s+time|historical\s+weather|weathering)\b",
        re.IGNORECASE,
    )

    @classmethod
    def is_volatile(cls, text: str) -> bool:
        """Returns True if the text requests changing or real-time information."""
        if not text:
            return False
        # If text explicitly matches safe static terminology, don't flag as volatile
        if cls._SAFE_EXCEPTIONS.search(text):
            # Check if it also genuinely matches stocks or live weather
            cleaned = cls._SAFE_EXCEPTIONS.sub("", text)
            return any(p.search(cleaned) for p in cls._PATTERNS.values())

        return any(p.search(text) for p in cls._PATTERNS.values())

    @classmethod
    def detect_category(cls, text: str) -> Optional[str]:
        """Returns the category name if volatile, or None."""
        if not text:
            return None
        cleaned = cls._SAFE_EXCEPTIONS.sub("", text) if cls._SAFE_EXCEPTIONS.search(text) else text
        for category, pattern in cls._PATTERNS.items():
            if pattern.search(cleaned):
                return category
        return None


# ============================================================================
# 2. Numerical Values Consistency Checker
# ============================================================================

class NumericalConsistencyChecker:
    """
    Extracts and compares numerical values between an incoming query and a cached prompt.
    Even slight numerical differences (e.g., N=10 vs N=20, 50% vs 60%, 2023 vs 2024)
    require completely different answers and must reject cache hits.
    """

    # Matches numbers: integers, decimals, negative numbers, percentages, fractions, scientific notation
    _NUMBER_REGEX = re.compile(
        r"(?<![a-zA-Z_])(?P<num>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?P<unit>%|km|ms|s|kg|mb|gb|tb|hz|khz|mhz|ghz)?(?![a-zA-Z_])",
        re.IGNORECASE,
    )

    @classmethod
    def extract_numbers(cls, text: str) -> List[Tuple[float, str]]:
        """
        Extracts all numbers from text as list of (float_value, unit) tuples.
        Standardizes representation for exact numeric comparisons.
        """
        if not text:
            return []
        results = []
        for match in cls._NUMBER_REGEX.finditer(text):
            num_str = match.group("num")
            unit_str = (match.group("unit") or "").lower()
            try:
                val = float(num_str)
                results.append((round(val, 6), unit_str))
            except ValueError:
                continue
        return results

    @classmethod
    def validate_numbers_match(cls, query_text: str, cached_text: str) -> Tuple[bool, str]:
        """
        Validates that numerical values in query_text match those in cached_text.
        Returns (is_valid, reason).
        """
        query_nums = cls.extract_numbers(query_text)
        cached_nums = cls.extract_numbers(cached_text)

        # If neither has numbers, check passes
        if not query_nums and not cached_nums:
            return True, "No numerical values present in either query"

        # If one has numbers and the other does not, mismatch!
        if bool(query_nums) != bool(cached_nums):
            return False, f"Numerical presence mismatch: query has {len(query_nums)} numbers, cached has {len(cached_nums)}"

        # Compare multisets / sorted lists of numbers
        sorted_q = sorted(query_nums)
        sorted_c = sorted(cached_nums)

        if sorted_q != sorted_c:
            return False, f"Numerical values mismatch: query={sorted_q} vs cached={sorted_c}"

        return True, "Numerical values match exactly"


# ============================================================================
# 3. Dimension & Attribute Opposition Checker
# ============================================================================

class DimensionOppositionChecker:
    """
    Prevents false cache hits for queries that share identical entities but ask about
    mutually exclusive dimensions or opposing attributes.
    Example:
      - 'What is the time complexity of binary search?'
      - 'What is the space complexity of binary search?'
    Both share 'complexity of binary search', but 'time' and 'space' are opposing dimensions!
    """

    OPPOSING_ATTRIBUTE_GROUPS = [
        # Algorithm complexity dimensions
        {"time", "space", "work", "depth", "communication", "message", "round"},
        # Asymptotic bounds
        {"worst", "worst-case", "best", "best-case", "average", "average-case", "amortized"},
        # Mathematical bounds & extremes
        {"upper", "lower"},
        {"minimum", "min", "maximum", "max", "infimum", "supremum"},
        # Order and monotonicity
        {"ascending", "descending", "increasing", "decreasing"},
        # Codec & Cryptography
        {"encode", "encoding", "decode", "decoding"},
        {"encrypt", "encryption", "decrypt", "decryption"},
        {"compress", "compression", "decompress", "decompression"},
        # Architecture and tiers
        {"cpu", "gpu", "tpu", "npu"},
        {"client", "server"},
        {"frontend", "backend"},
        {"hardware", "software"},
        {"user-space", "kernel-space"},
        # Concurrency & execution modes
        {"synchronous", "sync", "asynchronous", "async", "blocking", "non-blocking"},
        {"serial", "sequential", "parallel", "concurrent"},
        # Polarity & Inclusion
        {"with", "without"},
        {"positive", "negative"},
        {"internal", "external"},
        {"enable", "disable"},
        {"supervised", "unsupervised", "reinforcement", "semi-supervised"},
        {"primal", "dual"},
        {"convex", "non-convex", "concave"},
        {"pros", "cons", "advantage", "advantages", "disadvantage", "disadvantages"},
    ]

    @classmethod
    def _tokenize_words(cls, text: str) -> Set[str]:
        words = re.findall(r"\b[a-zA-Z0-9_\-]+\b", text.lower())
        return set(words)

    @classmethod
    def check_opposition(cls, query_text: str, cached_text: str) -> Tuple[bool, Optional[str]]:
        """
        Checks whether query_text and cached_text contain opposing attributes from the same group.
        Returns (is_valid, conflict_reason). is_valid is False if an opposition conflict is found.
        """
        words_q = cls._tokenize_words(query_text)
        words_c = cls._tokenize_words(cached_text)

        for group in cls.OPPOSING_ATTRIBUTE_GROUPS:
            q_attrs = words_q & group
            c_attrs = words_c & group

            # Both queries mention attributes in this group, but the attributes differ!
            if q_attrs and c_attrs and q_attrs != c_attrs:
                # E.g. query has {'time'}, cached has {'space'}
                diff_q = q_attrs - c_attrs
                diff_c = c_attrs - q_attrs
                if diff_q and diff_c:
                    conflict_desc = f"Opposing dimension conflict: query({diff_q}) vs cached({diff_c})"
                    return False, conflict_desc

        return True, None


# ============================================================================
# 4. Stage 2: Context and Metadata Validation
# ============================================================================

@dataclass
class ValidationResult:
    is_valid: bool
    stage: str
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class ContextAndMetadataValidator:
    """
    Stage 2 Validator:
    Verifies model version/tier, conversation context, language, relevant parameters,
    numerical consistency, and dimension opposition.
    """

    def __init__(self):
        self.num_checker = NumericalConsistencyChecker()
        self.dim_checker = DimensionOppositionChecker()

    def validate(
        self,
        query: str,
        cached_prompt: str,
        query_context: Optional[Dict[str, Any]] = None,
        cached_metadata: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        q_ctx = query_context or {}
        c_meta = cached_metadata or {}

        # 1. Model Tier / Version Validation
        q_model = q_ctx.get("model_tier") or q_ctx.get("model_version")
        c_model = c_meta.get("model_tier") or c_meta.get("model_version")
        if q_model and c_model and str(q_model).strip().lower() != str(c_model).strip().lower():
            return ValidationResult(
                is_valid=False,
                stage="stage2_context",
                reason=f"Model version/tier mismatch: requested={q_model} vs cached={c_model}",
            )

        # 2. Language Validation
        q_lang = str(q_ctx.get("language") or "en").strip().lower()
        c_lang = str(c_meta.get("language") or "en").strip().lower()
        if q_lang != c_lang:
            return ValidationResult(
                is_valid=False,
                stage="stage2_context",
                reason=f"Language mismatch: query={q_lang} vs cached={c_lang}",
            )

        # 3. Conversation Context Validation
        q_conv = q_ctx.get("conversation_context")
        c_conv = c_meta.get("conversation_context")
        if q_conv and c_conv and str(q_conv).strip() != str(c_conv).strip():
            return ValidationResult(
                is_valid=False,
                stage="stage2_context",
                reason=f"Conversation context mismatch: {q_conv} vs {c_conv}",
            )

        # 4. Numerical Values Consistency Validation
        num_ok, num_reason = self.num_checker.validate_numbers_match(query, cached_prompt)
        if not num_ok:
            return ValidationResult(
                is_valid=False,
                stage="stage2_numerical",
                reason=num_reason,
            )

        # 5. Dimension & Attribute Opposition Validation (e.g. time vs space complexity)
        dim_ok, dim_reason = self.dim_checker.check_opposition(query, cached_prompt)
        if not dim_ok:
            return ValidationResult(
                is_valid=False,
                stage="stage2_dimension",
                reason=dim_reason or "Opposing dimension conflict",
            )

        return ValidationResult(
            is_valid=True,
            stage="stage2_passed",
            reason="Context, metadata, numbers, and attributes validated successfully",
        )


# ============================================================================
# 5. Stage 3: Verification / Reranking (Cross-Encoder & LLM Judge)
# ============================================================================

class SemanticVerificationJudge:
    """
    Stage 3 Verifier:
    For uncertain matches (e.g. similarity between uncertainty thresholds or differing
    lexical phrasing), performs fine-grained verification using:
    1. SLM/LLM Judge (via prompt to local inference tier),
    2. Lightweight Cross-Encoder (if available), or
    3. Deterministic Lexical-Semantic Verifier (fast fallback).
    """

    _STOPWORDS = {
        "what", "is", "the", "of", "in", "on", "at", "for", "a", "an", "to",
        "and", "or", "by", "with", "how", "why", "when", "where", "which",
        "can", "could", "should", "would", "do", "does", "did", "are", "be",
        "been", "being", "please", "explain", "describe", "tell", "me", "about",
    }

    _INTERROGATIVES = {"what", "why", "how", "when", "where", "who", "which", "compare", "calculate", "prove"}

    @classmethod
    def _extract_content_words(cls, text: str) -> Set[str]:
        words = re.findall(r"\b[a-zA-Z0-9_\-]{2,}\b", text.lower())
        return {w for w in words if w not in cls._STOPWORDS}

    @classmethod
    def verify_lexical_semantic(
        cls, query: str, cached_prompt: str, similarity: float
    ) -> Tuple[bool, float, str]:
        """
        Deterministic zero-latency fallback verifier:
        Checks question intent alignment, negation consistency, and content word overlap.
        """
        words_q = cls._extract_content_words(query)
        words_c = cls._extract_content_words(cached_prompt)

        # If both are empty or trivial
        if not words_q and not words_c:
            return True, 1.0, "trivial_match"

        # Interrogative intent check
        tokens_q = set(re.findall(r"\b[a-zA-Z]+\b", query.lower()))
        tokens_c = set(re.findall(r"\b[a-zA-Z]+\b", cached_prompt.lower()))
        q_interrogatives = tokens_q & cls._INTERROGATIVES
        c_interrogatives = tokens_c & cls._INTERROGATIVES

        if q_interrogatives and c_interrogatives and not (q_interrogatives & c_interrogatives):
            # E.g. "how to calculate" vs "why is"
            return False, 0.2, f"Interrogative intent mismatch: {q_interrogatives} vs {c_interrogatives}"

        # Negation check
        negations = {"not", "never", "no", "cannot", "cant", "wont", "neither", "nor"}
        q_neg = bool(tokens_q & negations)
        c_neg = bool(tokens_c & negations)
        if q_neg != c_neg:
            return False, 0.0, "Negation polarity mismatch"

        # Content word Jaccard similarity
        intersection = len(words_q & words_c)
        union = len(words_q | words_c)
        jaccard = intersection / union if union > 0 else 1.0

        # Weighted verification score: combines dense cosine similarity (70%) + lexical overlap (30%)
        verification_score = 0.70 * similarity + 0.30 * jaccard

        if jaccard < 0.40 and similarity < 0.95:
            return False, verification_score, f"Low lexical content overlap: Jaccard={jaccard:.2f}"

        return True, verification_score, f"Lexical-semantic verified: Jaccard={jaccard:.2f}, Score={verification_score:.2f}"

    @classmethod
    async def verify_with_slm(
        cls, query: str, cached_prompt: str, slm_client: Any
    ) -> Tuple[bool, str]:
        """
        Prompts an on-premise SLM to judge if two questions have identical semantic intent.
        """
        if slm_client is None:
            return True, "No SLM client provided; skipped"

        prompt_messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict semantic equivalence judge for a database cache. "
                    "Your job is to determine if Question A and Question B ask for the exact same information "
                    "such that the answer to Question B is completely valid and accurate for Question A.\n"
                    "Reply with ONLY 'YES' or 'NO'."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question A: {query}\n"
                    f"Question B: {cached_prompt}\n"
                    "Do they expect the exact same answer? (YES/NO):"
                ),
            },
        ]

        try:
            resp = await slm_client.generate_chat(prompt_messages, max_tokens=10, temperature=0.0)
            content = ""
            if resp and "choices" in resp and resp["choices"]:
                content = resp["choices"][0]["message"]["content"].strip().upper()
            if content.startswith("YES"):
                return True, "SLM judge confirmed semantic equivalence"
            elif content.startswith("NO"):
                return False, "SLM judge rejected: questions differ in intent or answer"
            # If ambiguous, fall back to lexical-semantic verification
            return True, f"SLM returned ambiguous response: {content}"
        except Exception as exc:
            logger.warning("SLM judge call failed; falling back to heuristic: %s", exc)
            return True, f"SLM fallback: {exc}"


# ============================================================================
# 6. Unified Three-Stage Cache Validator
# ============================================================================

class ThreeStageCacheValidator:
    """
    Orchestrates the three-stage validation pipeline:
    - Stage 1: Semantic similarity filtering (KNN cosine distance)
    - Stage 2: Context, metadata, numerical consistency, and dimension opposition validation
    - Stage 3: Verification & reranking for uncertain matches (SLM judge or lexical verifier)
    Also enforces the Volatile Information Gate to avoid caching changing data.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        confident_threshold: float = 0.96,
        uncertainty_threshold_low: float = 0.85,
        uncertainty_threshold_high: float = 0.96,
        enable_volatile_guard: bool = True,
        enable_stage2: bool = True,
        enable_stage3: bool = True,
    ):
        self.similarity_threshold = float(similarity_threshold)
        self.confident_threshold = float(confident_threshold)
        self.uncertainty_threshold_low = float(uncertainty_threshold_low)
        self.uncertainty_threshold_high = float(uncertainty_threshold_high)
        self.enable_volatile_guard = enable_volatile_guard
        self.enable_stage2 = enable_stage2
        self.enable_stage3 = enable_stage3

        self.volatile_detector = VolatileQueryDetector()
        self.stage2_validator = ContextAndMetadataValidator()
        self.stage3_judge = SemanticVerificationJudge()

    def is_volatile(self, query: str) -> bool:
        """Check if query requests rapidly changing / real-time info."""
        if not self.enable_volatile_guard:
            return False
        return self.volatile_detector.is_volatile(query)

    async def validate_candidate(
        self,
        query: str,
        cached_prompt: str,
        similarity: float,
        query_context: Optional[Dict[str, Any]] = None,
        cached_metadata: Optional[Dict[str, Any]] = None,
        slm_client: Optional[Any] = None,
        freshness_handled: bool = False,
    ) -> ValidationResult:
        """
        Executes the validation pipeline on a retrieved cache candidate.
        """
        # Step 0: Volatile Information Check
        if self.enable_volatile_guard and not freshness_handled and self.is_volatile(query):
            cat = self.volatile_detector.detect_category(query) or "volatile_data"
            return ValidationResult(
                is_valid=False,
                stage="volatile_gate",
                reason=f"Query involves changing/volatile information ({cat}); caching bypassed",
            )

        # Stage 1: Semantic Similarity Filter
        if similarity < self.similarity_threshold:
            return ValidationResult(
                is_valid=False,
                stage="stage1_similarity",
                reason=f"Similarity {similarity:.4f} below threshold {self.similarity_threshold:.4f}",
            )

        # Stage 2: Context and Metadata Validation
        if self.enable_stage2:
            st2_res = self.stage2_validator.validate(
                query=query,
                cached_prompt=cached_prompt,
                query_context=query_context,
                cached_metadata=cached_metadata,
            )
            if not st2_res.is_valid:
                return st2_res

        # Stage 3: Optional Reranking or Verification for Uncertain Matches
        if self.enable_stage3:
            # A match is uncertain if similarity is below confident_threshold
            # or if content words have any differences
            words_q = self.stage3_judge._extract_content_words(query)
            words_c = self.stage3_judge._extract_content_words(cached_prompt)
            is_exact_content = (words_q == words_c)

            is_uncertain = (similarity < self.confident_threshold) or not is_exact_content

            if is_uncertain:
                # 1. Deterministic lexical-semantic verifier
                lex_ok, score, lex_reason = self.stage3_judge.verify_lexical_semantic(
                    query, cached_prompt, similarity
                )
                if not lex_ok:
                    return ValidationResult(
                        is_valid=False,
                        stage="stage3_verification",
                        reason=f"Stage 3 lexical-semantic verification rejected: {lex_reason}",
                    )

                # 2. SLM Judge verification (if slm_client available and match is borderline)
                if slm_client is not None and similarity < self.confident_threshold:
                    slm_ok, slm_reason = await self.stage3_judge.verify_with_slm(
                        query, cached_prompt, slm_client
                    )
                    if not slm_ok:
                        return ValidationResult(
                            is_valid=False,
                            stage="stage3_slm_judge",
                            reason=f"Stage 3 SLM judge rejected: {slm_reason}",
                        )

        return ValidationResult(
            is_valid=True,
            stage="stage3_approved",
            reason="Passed all 3 validation stages (similarity, context/numbers/attributes, verification)",
            metadata={"similarity": similarity},
        )

