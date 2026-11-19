"""DEFECT-40: the typo normaliser never turns a real word into a different one (Sub-FR-1.1).

Reported: graphane/graphyne -> graphene, ferrielectric -> ferroelectric, neutrons -> neurons (no system word list),
so distinct questions shared one exact cache key and graphane was answered with the graphene answer. Now a word is
protected if it is in the English dictionary bundled with symspellpy or appears anywhere in the thesis corpus;
when the normaliser does change a question, the stream says what was searched for.
"""

import pytest

import pipeline.text_processing.typo_normalizer as tn
from pipeline.text_processing.typo_normalizer import CorpusUnavailable, TypoNormalizer


@pytest.fixture
def minimal_install(monkeypatch):
    """No /usr/share/dict/words: only the hard-coded fallback list, as on the red team's host."""
    t = TypoNormalizer()
    monkeypatch.setattr(t, "valid_english_words", set(TypoNormalizer.FALLBACK_VALID_WORDS))
    t._correct_token_checked.cache_clear()
    return t


def corpus(monkeypatch, words=(), down=False):
    def fake(word):
        if down:
            raise CorpusUnavailable("redis down")
        return word in words
    monkeypatch.setattr(tn, "_in_corpus", fake)


def test_common_words_are_protected_without_a_system_word_list(minimal_install, monkeypatch):
    corpus(monkeypatch)
    for w in ("neutrons", "neutron", "fluids", "magnets"):
        assert minimal_install.normalize(w) == w


def test_corpus_terms_are_protected(minimal_install, monkeypatch):
    corpus(monkeypatch, words={"graphane", "graphyne"})
    assert minimal_install.normalize("graphane field effect transistor") == "graphane field effect transistor"
    assert minimal_install.normalize("graphyne") == "graphyne"


def test_genuine_typos_are_still_corrected(minimal_install, monkeypatch):
    corpus(monkeypatch)
    assert minimal_install.normalize("neroscience") == "neuroscience"
    assert minimal_install.normalize("grapheen") == "graphene"


def test_distinct_terms_no_longer_share_the_exact_cache_key(minimal_install, monkeypatch):
    corpus(monkeypatch, words={"graphane"})
    assert minimal_install.normalize("What is graphane used for?") != minimal_install.normalize("What is graphene used for?")


def test_decisions_made_without_the_corpus_are_not_cached(minimal_install, monkeypatch):
    # neurone: a real spelling protected only by the corpus (graphane is now protected by its shape, DEFECT-62)
    corpus(monkeypatch, down=True)
    assert minimal_install.normalize("neurone") == "neurons"        # index unavailable: dictionary only
    corpus(monkeypatch, words={"neurone"})
    assert minimal_install.normalize("neurone") == "neurone", "protected once the index is reachable again"


def test_bridge_forwards_the_correction_notice():
    from askiisc_chat.askiisc import gateway_backend as gb
    src = open(gb.__file__).read()
    assert 'elif t == "correction":' in src


# ------------------------------------------------------------------------------ live corpus check
def test_live_corpus_protects_reported_terms():
    from tests._live import INDEX_REASON, redis_available
    if not redis_available():
        pytest.skip(INDEX_REASON)
    tn._in_corpus.cache_clear()
    try:
        present = tn._in_corpus("graphane")
    except CorpusUnavailable:
        pytest.skip(INDEX_REASON)
    if not present:
        pytest.skip("thesis index without the reported terms")
    t = TypoNormalizer()
    assert t.normalize("graphane graphyne neutrons") == "graphane graphyne neutrons"


# ------------------------------------------------- semantic hits across near-spelled distinct terms
from gateway.cache.redis_semantic_cache import RedisSemanticCache as C


@pytest.mark.parametrize("a,b", [("What are graphane transistors used for?", "What are graphene transistors used for?"),
                                 ("How are neutrons detected?", "How are neurons detected?"),
                                 ("Explain ferrielectric ordering", "Explain ferroelectric ordering")])
def test_semantic_hit_refused_between_distinct_terms(monkeypatch, a, b):
    corpus(monkeypatch, words={"graphane", "graphene", "ferroelectric"})
    import pipeline.text_processing.typo_normalizer as t2
    monkeypatch.setattr(t2, "_is_english", lambda w: w in {"neutrons", "neurons", "ferrielectric"})
    assert C.check_entity_conflict(a, b)[0]


@pytest.mark.parametrize("a,b", [("What are graphene transistors used for?", "What are grapheen transistors used for?"),
                                 ("Explain transformer models", "Explain transformers models")])
def test_typos_and_plurals_still_hit(monkeypatch, a, b):
    corpus(monkeypatch, words={"graphene", "transformer", "transformers"})
    import pipeline.text_processing.typo_normalizer as t2
    monkeypatch.setattr(t2, "_is_english", lambda w: w in {"explain", "models"})
    assert not C.check_entity_conflict(a, b)[0]


# ------------------------------------------- coinages absent from the corpus (DEFECT-62, follow-up to DEFECT-40)
@pytest.mark.parametrize("term", ["ferrielectric", "graphane", "graphyne", "nanographene", "antiferroelectric",
                                  "magnetoelectric", "germanene"])
def test_coinages_are_kept_even_when_the_corpus_lacks_them(minimal_install, monkeypatch, term):
    """ferrielectric appears in 0 production thesis chunks, and a small corpus may lack graphane / graphyne: the
    dictionary and corpus checks saw typos and rewrote them. Words built from known parts are now kept."""
    corpus(monkeypatch, words=())
    assert tn.looks_like_term(term, minimal_install.vocab)      # nanographene: nano + graphene (a vocabulary term)
    assert minimal_install.correct_word(term) == term


@pytest.mark.parametrize("typo,fixed", [("grapheen", "graphene"), ("neroscience", "neuroscience"),
                                        ("maerials", "materials"), ("comustion", "combustion"),
                                        ("dielectic", "dielectric"), ("experimentol", "experimental")])
def test_typos_are_not_mistaken_for_coinages(minimal_install, monkeypatch, typo, fixed):
    corpus(monkeypatch, words=())
    assert not tn.looks_like_term(typo, minimal_install.vocab)
    assert minimal_install.correct_word(typo) == fixed


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


class _ExactCache:
    """Returns the stored entry for the corrected text as an exact hit (the fast path skips the intent guard)."""
    def __init__(self, stored_prompt):
        self.stored, self.calls = stored_prompt, []

    async def alookup(self, q, owner=None):
        self.calls.append(q)
        if q == self.stored:
            return {"cached": True, "similarity": 1.0, "fast_exact": True, "matched_prompt": self.stored,
                    "response": {"answer": "stored", "chunks": [{"doc_id": "d1"}]}}
        return None


@pytest.mark.parametrize("typed,corrected,stored,served", [
    # a rewrite into another real term: refused (no other term's answer at similarity 1.0)
    ("What are graphane transistors used for?", "What are graphene transistors used for?",
     "What are graphene transistors used for?", False),
    ("Explain ferrielectric ordering in thin films", "Explain ferroelectric ordering in thin films",
     "Explain ferroelectric ordering in thin films", False),
    # a typo: still served from the cache (Sub-FR-1.1)
    ("What are grapheen transistors used for?", "What are graphene transistors used for?",
     "What are graphene transistors used for?", True),
    ("what is neroscience?", "what is neuroscience?", "what is neuroscience?", True),
])
def test_exact_hits_for_a_rewritten_question_run_the_intent_guard(monkeypatch, typed, corrected, stored, served):
    """The reporter's suggestion: run the entity guard on the exact path too. Even if a coinage were rewritten,
    the exact hit for the corrected text is checked against the question as typed."""
    import gateway.main as gm
    corpus(monkeypatch, words={"graphene", "transistors", "ferroelectric", "neuroscience"})
    cache = _ExactCache(stored)
    hit = _run(gm._cache_lookup(cache, corrected, typed, "alice@iisc.ac.in"))
    assert (hit is not None) is served
    if not served:
        assert cache.calls == [corrected, typed]    # refused, then looked up as typed (a miss here)
