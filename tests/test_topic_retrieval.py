"""Keyword-assisted retrieval for short topic questions.

User report: "what is transformers" answered (from unrelated chunks just above the 0.52 floor) while
"explain transformers" was refused (nothing above the floor). Both phrasings must now retrieve the
same thesis chunks that actually contain the topic word, without stemming noise.
"""

import asyncio

import pytest

from pipeline.retriever import RedisChunkRetriever as R


@pytest.mark.parametrize("query,terms", [
    ("explain transformers", ["transformers"]),
    ("what is transformers", ["transformers"]),
    ("What are transformers?", ["transformers"]),
    ("Describe transformers in detail", ["transformers"]),
    ("Can you please explain graphene", ["graphene"]),
    ("Tell me about GMRES", ["gmres"]),
    ("Explain the working of a lithium-ion battery", ["lithium", "ion", "battery"]),
    ("transformers", ["transformers"]),
    ("give an overview of perovskite solar cells", ["perovskite", "solar", "cells"]),
])
def test_topic_terms_extracted(query, terms):
    assert R.topic_terms(query) == terms


@pytest.mark.parametrize("query", [
    "What is the role of mycorrhizal fungi in plant nutrition?",
    "Who supervised the thesis on graphene?",
    "How does the flare angle affect flame stability in high-shear injectors?",
    "Compare GMRES and MINRES for non-symmetric systems",
    "",
    "explain",
])
def test_full_questions_are_not_topic_queries(query):
    assert R.topic_terms(query) is None


def test_term_variants_cover_singular_and_plural():
    assert R._term_variants("transformers") == ["transformer", "transformers"]
    assert R._term_variants("transformer") == ["transformer", "transformers"]
    assert set(R._term_variants("battery")) == {"battery", "batteries"}
    assert "property" in R._term_variants("properties")
    assert "transformation" not in R._term_variants("transformer"), "no stemming: different words stay apart"


@pytest.fixture(scope="module")
def retriever():
    r = R()
    try:
        r.search("warm up query", top_k=1)
    except Exception as exc:
        pytest.skip(f"retriever unavailable: {exc}")
    return r


def _titles(chunks):
    return [str(c.get("title") or "").lower() for c in chunks]


def test_both_phrasings_retrieve_the_same_on_topic_theses(retriever):
    a = retriever.search("explain transformers")
    b = retriever.search("what is transformers")
    assert a and b, "both phrasings must retrieve chunks (one used to be refused)"
    assert all(c.get("retrieval_mode") == "keyword_assisted" for c in a + b)
    assert all("transformer" in t for t in _titles(a)[:3] + _titles(b)[:3]), (_titles(a), _titles(b))
    assert set(_titles(a)) & set(_titles(b))


def test_verbatim_matching_avoids_stemming_noise(retriever):
    titles = _titles(retriever.search("explain transformers", top_k=10))
    assert not any("transformation" in t or "fourier transform" in t for t in titles), titles


def test_title_matches_ranked_first(retriever):
    res = retriever.search("explain GMRES")
    assert res and res[0].get("keyword_title_match") is True


def test_async_path_matches_sync(retriever):
    sync = [(c.get("title"), c.get("similarity")) for c in retriever.search("explain transformers")]
    asy = [(c.get("title"), c.get("similarity")) for c in asyncio.run(retriever.asearch("explain transformers"))]
    assert sync == asy


def test_full_questions_keep_the_dense_path(retriever):
    res = retriever.search("What is the role of mycorrhizal fungi in plant nutrition?")
    # not the short-topic keyword path; chunks found via the rare word "mycorrhizal" are a separate,
    # corroborated addition (tests/test_rare_terms.py)
    assert all(c.get("retrieval_mode") in (None, "rare_term") for c in res)
    assert any(c.get("retrieval_mode") is None for c in res), "the dense results are still there"


def test_unknown_topic_falls_back_safely(retriever):
    res = retriever.search("explain zqxwvutsrq")
    assert isinstance(res, list)
    assert all(c.get("retrieval_mode") is None for c in res)


@pytest.mark.parametrize("query", ["what is the meaning of life", "what is love", "explain happiness", "what is energy"])
def test_common_or_out_of_domain_topics_do_not_get_the_lower_floor(retriever, query):
    for c in retriever.search(query):
        assert float(c.get("similarity", 0)) >= retriever.min_similarity, (query, c.get("similarity"), c.get("title"))


def test_specificity_gate():
    r = R.__new__(R)
    assert r._is_specific(1820) and r._is_specific(503)
    assert not r._is_specific(12606) and not r._is_specific(0)



@pytest.mark.parametrize("query,fix", [
    ("explain transformerss", {"transformerss": "transformers"}),
    ("explain transfomers", {"transfomers": "transformers"}),
    ("explain gmress", {"gmress": "gmres"}),
])
def test_misspelled_topics_are_corrected_and_retrieved(retriever, query, fix):
    res = retriever.search(query)
    assert res, f"{query!r} must not be refused"
    assert res[0].get("keyword_corrections") == fix
    word = list(fix.values())[0].rstrip("s")
    assert word in str(res[0].get("title", "")).lower()


def test_singular_and_plural_retrieve_on_topic_theses(retriever):
    for q in ("explain transformer", "explain transformers"):
        assert any("transformer" in t for t in _titles(retriever.search(q))[:3]), q
