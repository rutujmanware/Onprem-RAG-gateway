"""Library-based spelling correction and base words for topic questions (symspellpy + lemminflect)."""

import pytest

from pipeline.text_processing.topic_speller import TITLE_TERMS, TopicSpeller

sp = TopicSpeller.get_instance()
pytestmark = pytest.mark.skipif(sp.sym is None, reason="symspellpy not installed")


def test_title_dictionary_present_and_clean():
    assert TITLE_TERMS.exists(), "run scripts/build_spell_dictionary.py"
    assert {"transformer", "gmres", "perovskite", "graphene"} <= set(sp.title_terms)
    assert "graphen" not in sp.title_terms, "OCR fragments from thesis bodies must not be in the dictionary"


@pytest.mark.parametrize("wrong,right", [
    ("transformerss", "transformers"), ("transfomers", "transformers"), ("gmress", "gmres"),
    ("grapheen", "graphene"), ("perovskitte", "perovskite"), ("lithum", "lithium"),
])
def test_misspellings_corrected_to_corpus_words(wrong, right):
    assert sp.correct(wrong) == right


@pytest.mark.parametrize("word", ["transformers", "transformer", "life", "stress", "batteries", "perovskite", "gmres", "the"])
def test_real_words_left_alone(word):
    assert sp.correct(word) is None


def test_short_or_non_alpha_words_not_corrected():
    assert sp.correct("xyz") is None and sp.correct("c4") is None


@pytest.mark.parametrize("word,lemma", [("transformers", "transformer"), ("batteries", "battery"),
                                        ("analyses", "analysis"), ("theses", "thesis"), ("transformation", "transformation")])
def test_lemmas(word, lemma):
    assert sp.lemma(word) == lemma


def test_correct_terms_reports_fixes():
    terms, fixes = sp.correct_terms(["gmress", "battery"])
    assert terms == ["gmres", "battery"] and fixes == {"gmress": "gmres"}


def test_correction_is_fast():
    import time
    t0 = time.perf_counter()
    for _ in range(50):
        sp.correct("transformerss")
    assert (time.perf_counter() - t0) / 50 < 0.05


def test_framing_words_are_not_topic_terms():
    """Every topic term must appear in a thesis title, so "known"/"theses" left in matched nothing (2026-11-14)."""
    from pipeline.retriever import RedisChunkRetriever as R
    assert R.topic_terms("What is known about GMRES?") == ["gmres"]
    assert R.topic_terms("What is known about GMRES in the theses?") == ["gmres"]
    assert R.topic_terms("Explain IISc research on mycorrhizal fungi") == ["mycorrhizal", "fungi"]
