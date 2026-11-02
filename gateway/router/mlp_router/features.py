import math
import numpy as np

# Intent keywords indicating computational, synthesis, or reasoning complexity (Target: GPU vLLM)
HARD_KEYWORDS = [
    "compare", "comparison", "contrast", "versus", " vs ", "tradeoff", "trade-off", "trade-offs",
    "difference between", "synthesize", "synthesis", "integrate", "holistic", "evaluate", "critique",
    "derive", "proof", "prove", "algorithm", "architecture", "mechanism", "implement", "mathematical",
    "deduce", "optimize", "optimization", "throughput", "latency", "bottleneck", "distinguish between",
    "relationship between", "multi-step", "tradeoffs", "compared to", "differ from", "whereas"
]

# Prefixes indicating routine direct factual lookup (Target: CPU SLM)
SIMPLE_PREFIXES = [
    "what is", "what are", "who is", "who was", "where is", "when was", "when did",
    "define", "name the", "list the", "state the", "contact", "prerequisite", "prerequisites",
    "how many", "which of the following"
]

HARD_PREFIXES = [
    "compare", "contrast", "synthesize", "evaluate", "derive", "prove", "explain how",
    "how do the methodologies", "how does the", "analyze the", "critique"
]

NUM_LEXICAL_FEATURES = 16

def extract_lexical_features(query: str, embedding: np.ndarray = None) -> np.ndarray:
    """
    Extract a normalized 16-dimensional lexical and structural feature vector
    strictly and purely from the user's incoming query text (and its dense embedding).
    
    Operates in < 0.05 ms with zero external calls or database lookups.
    """
    q_clean = query.strip()
    q_lower = q_clean.lower()
    words = q_clean.split()
    num_words = len(words)
    num_chars = len(q_clean)

    # 1. Structural features
    f_word_count = min(num_words / 40.0, 1.0)
    f_char_count = min(num_chars / 250.0, 1.0)
    f_avg_word_len = min((num_chars / max(1, num_words)) / 10.0, 1.0)

    # 2. Punctuation complexity (multi-clause / multi-step questions)
    clauses = q_clean.count(',') + q_clean.count(';') + q_clean.count(':') + q_clean.count('-')
    f_clauses = min(clauses / 4.0, 1.0)
    f_questions = min(q_clean.count('?'), 2) / 2.0

    # 3. Intent & Prefix detection
    f_starts_with_simple = 1.0 if any(q_lower.startswith(p) for p in SIMPLE_PREFIXES) else 0.0
    f_starts_with_hard = 1.0 if any(q_lower.startswith(p) for p in HARD_PREFIXES) else 0.0

    f_has_compare = 1.0 if any(k in q_lower for k in [
        "compare", "comparison", "contrast", "versus", " vs ", "tradeoff",
        "trade-off", "trade-offs", "difference between", "differ from"
    ]) else 0.0

    f_has_synthesis = 1.0 if any(k in q_lower for k in [
        "synthesize", "synthesis", "integrate", "holistic", "evaluate", "critique"
    ]) else 0.0

    f_has_algorithmic = 1.0 if any(k in q_lower for k in [
        "derive", "proof", "prove", "algorithm", "architecture", "mechanism", "implement", "deduce"
    ]) else 0.0

    f_has_optimize = 1.0 if any(k in q_lower for k in [
        "optimize", "optimization", "throughput", "latency", "bottleneck", "tradeoffs"
    ]) else 0.0

    f_has_contrastive_link = 1.0 if any(k in q_lower for k in [
        " compared to ", " whereas ", " rather than ", " while ", " in contrast to "
    ]) else 0.0

    f_hard_density = min(sum(1.0 for k in HARD_KEYWORDS if k in q_lower) / 3.0, 1.0)
    f_has_math = 1.0 if any(c in "+=*^/<>$\\" for c in q_clean) else 0.0
    f_has_quote = 1.0 if ('"' in q_clean or "'" in q_clean or "[" in q_clean) else 0.0

    # 4. Dense Embedding Statistics (if provided)
    if embedding is not None:
        f_emb_std = min(float(np.std(embedding)) * 10.0, 1.0)
    else:
        f_emb_std = 0.0

    return np.array([
        f_word_count, f_char_count, f_avg_word_len,
        f_clauses, f_questions,
        f_starts_with_simple, f_starts_with_hard,
        f_has_compare, f_has_synthesis, f_has_algorithmic,
        f_has_optimize, f_has_contrastive_link,
        f_hard_density, f_has_math, f_has_quote,
        f_emb_std
    ], dtype=np.float32)

def extract_hybrid_features(query: str, embedding: np.ndarray) -> np.ndarray:
    """
    Combines the 384-dimensional dense semantic embedding with the 16-dimensional
    explicit lexical/intent vector to create a 400-dimensional hybrid representation.
    """
    lex_feats = extract_lexical_features(query, embedding)
    return np.concatenate([embedding, lex_feats], axis=-1)
