"""Rare query words (2026-11-15): a rare word the user typed ("GMRES") is evidence by itself.

The embedding model does not know such words, so "What is known about GMRES?" scored 0.44 against the GMRES
thesis, below the 0.52 floor, and returned nothing. Chunks containing a rare word (<= RARE_MAX_CHUNKS of the
index) are now fetched with a filtered vector search and accepted from RARE_MIN_SIMILARITY, if corroborated
(>= 2 chunks of that thesis, or the word in its title). The global floor is unchanged.

Measured on a frozen rare-term set (logs/retrieval_eval/rare_questions.jsonl): hit@3 0.467 -> 0.758,
35 questions better, 0 worse; the main 244-question set unchanged (hit@3 0.691 -> 0.691).
"""

import pytest

from pipeline.retriever import RedisChunkRetriever as R


def chunk(doc, i, sim, title="Some thesis"):
    return {"doc_id": doc, "chunk_index": i, "content": f"{doc} chunk {i}", "similarity": sim, "title": title}


@pytest.fixture
def ret():
    obj = R.__new__(R)  # no Redis / embedder needed for the selection logic
    return obj


def test_candidates_skip_stopwords_framing_and_everyday_english():
    assert R.rare_candidates("What is known about GMRES in the theses?") == ["gmres"]
    c = R.rare_candidates("How does increasing the temperature affect the system?")
    assert not {"increasing", "temperature", "system", "known", "the", "does"} & set(c)


def test_rarest_words_are_picked_within_the_limit(ret):
    picked = ret._pick_rare([("gmres", 524), ("minres", 6), ("graphene", 34226), ("absent", 0), ("kfvs", 1753)])
    assert picked == ["minres", "gmres"]  # rarest first, at most RARE_MAX_TERMS, never df 0 or > RARE_MAX_CHUNKS


def test_floor_and_corroboration(ret):
    hits = [("gmres", [
        chunk("A", 1, 0.44), chunk("A", 2, 0.41),          # corroborated: 2 chunks of thesis A
        chunk("B", 1, 0.43),                                 # lone mention (citation): dropped
        chunk("C", 1, 0.35, title="GMRES acceleration"),     # lone, but the word is in the title: kept
        chunk("D", 1, 0.20), chunk("D", 2, 0.25),            # below RARE_MIN_SIMILARITY: dropped
    ])]
    kept = ret._select_rare_hits(hits)
    got = {(c["doc_id"], c["chunk_index"]) for c in kept}
    assert ("B", 1) not in got and ("D", 1) not in got and ("D", 2) not in got
    assert ("C", 1) in got
    assert len(kept) <= R.RARE_MAX_ADDED
    assert all(c["retrieval_mode"] == "rare_term" and c["rare_term"] == "gmres" for c in kept)


def test_merge_ranks_rare_chunks_with_bonus_and_drops_duplicates(ret):
    rare = [chunk("G", 1, 0.44)]
    dense = [chunk("X", 1, 0.56), chunk("G", 1, 0.44), chunk("Y", 1, 0.70)]
    merged = ret._merge_rare(rare, dense, top_k=None)
    ids = [(c["doc_id"], c["chunk_index"]) for c in merged]
    assert ids.count(("G", 1)) == 1
    assert ids.index(("G", 1)) < ids.index(("X", 1))   # 0.44 + bonus beats 0.56
    assert ids[0] == ("Y", 1)                            # a strong normal match still ranks first
    assert len(ret._merge_rare(rare, dense, top_k=2)) == 2
    assert ret._merge_rare([], dense, top_k=None) is dense


# ----------------------------------------------------------------------------------- live (index needed)
@pytest.fixture(scope="module")
def live():
    r = R()
    try:
        r.search("explain transformers")
    except Exception as e:
        pytest.skip(f"Redis index not available: {e}")
    return r


def test_live_rare_acronym_question_retrieves_its_thesis(live):
    hits = live.search("What is known about GMRES?")
    assert hits and any("gmres" in str(h.get("title", "")).lower() for h in hits[:3])


def test_live_off_corpus_still_retrieves_nothing(live):
    assert live.search("What is the recipe for butter chicken?") == []


def test_live_switch_off_restores_previous_behaviour(live, monkeypatch):
    monkeypatch.setattr(R, "RARE_TERMS_ENABLED", False)
    assert all(h.get("retrieval_mode") != "rare_term" for h in live.search("What is known about GMRES?"))
