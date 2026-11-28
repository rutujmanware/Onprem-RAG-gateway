"""Sources list the [Document n] numbers they cover (2026-11-27).

Several passages of one thesis are merged into one source. An answer that cited [Document 2] then showed a single
source ("what is Markov chain": 5 passages, all from one thesis) and the reader could not tell where Document 2 was."""

from pathlib import Path

from askiisc_chat.askiisc import gateway_backend as gb
from pipeline.retriever import sanitize_chunks
from pipeline.sanitizer import fence_context_chunks

ROOT = Path(__file__).resolve().parent.parent
CHUNKS = [
    {"title": "Markov Chain Splitting", "content": "p1", "similarity": 0.61, "chunk_key": "k1"},
    {"title": "Markov Chain Splitting", "content": "p2", "similarity": 0.58},
    {"title": "Hidden Markov Models", "content": "p3", "similarity": 0.57},
    {"title": "Markov Chain Splitting", "content": "p4", "similarity": 0.56},
]


def test_merged_source_keeps_every_document_number():
    out = sanitize_chunks(CHUNKS)
    assert [(c["title"], c["documents"]) for c in out] == [
        ("Markov Chain Splitting", [1, 2, 4]), ("Hidden Markov Models", [3])]
    assert all("content" not in c and "chunk_key" not in c for c in out), "still no chunk text or internal keys"


def test_numbers_match_the_prompt_numbering():
    fenced = fence_context_chunks(CHUNKS)
    for c in sanitize_chunks(CHUNKS):
        for n in c["documents"]:
            assert f'<document id="{n}" title="{c["title"]}">' in fenced


def test_web_chat_bridge_passes_and_merges_numbers():
    item = gb.cite(1, {"title": "T", "documents": [2, 1, "3", True, 0, 99]})
    assert item["documents"] == [2, 1], "only plausible integers reach the browser"
    assert gb.cite(1, {"title": "T"})["documents"] == []


def test_page_shows_and_merges_numbers():
    js = (ROOT / "askiisc_chat" / "static" / "app.js").read_text()
    assert "function docNumbers(c)" in js
    assert "'Documents' : 'Document'} ${docs.join(', ')}" in js   # shown on each source card
    assert "docNumbers(c).forEach(n => { if (!docs.includes(n)) docs.push(n); })" in js   # merged on de-dup
    assert "documents: docNumbers(c)" in js   # kept in saved chats


def test_cached_sources_keep_their_numbers():
    stored = sanitize_chunks(CHUNKS)              # what the cache stores
    again = sanitize_chunks(stored)               # what a cache hit serves
    assert [c["documents"] for c in again] == [[1, 2, 4], [3]], "a cache hit must not renumber"
    old_entry = [{"title": "Markov Chain Splitting"}, {"title": "Hidden Markov Models"}]   # cached before numbers
    assert [c["documents"] for c in sanitize_chunks(old_entry)] == [[], []], "unknown, not invented"


# ------------------------------------------------------------------ several theses in the first places
from pipeline.retriever import RedisChunkRetriever as _R


def _c(doc, sim, title_match=None):
    c = {"doc_id": doc, "title": doc, "similarity": sim}
    if title_match is not None:
        c["keyword_title_match"] = title_match
    return c


def test_diversify_round_robins_by_thesis_and_keeps_order():
    got = _R.diversify([_c("A", .9), _c("A", .8), _c("A", .7), _c("B", .6), _c("C", .5), _c("B", .4)])
    assert [(c["doc_id"], c["similarity"]) for c in got] == [
        ("A", .9), ("B", .6), ("C", .5), ("A", .8), ("B", .4), ("A", .7)]
    one = [_c("A", .9), _c("A", .8)]
    assert _R.diversify(one) == one


def test_keyword_ranking_caps_title_matches_per_thesis_before_content_matches(monkeypatch):
    r = _R.__new__(_R)
    r.min_similarity, r.KEYWORD_MIN_SIMILARITY = 0.52, 0.45
    monkeypatch.setattr(_R, "_term_variants", staticmethod(lambda t: [t]))
    # "what is hidden markov model": 3 title chunks of one thesis (0.45-0.53) ahead of better content matches
    hits = [{"doc_id": "rain", "title": "Rainfall hidden Markov model", "similarity": s} for s in (.528, .526, .45)] + \
           [{"doc_id": "prot", "title": "Protein sequences", "similarity": .721},
            {"doc_id": "venom", "title": "Venominformatics", "similarity": .617}]
    got = [c["doc_id"] for c in r._rank_keyword_hits(hits, ["markov"], 5, specific=True)]
    assert got == ["rain", "rain", "prot", "venom", "rain"], "the CPU tier's first 3 now hold 2 theses"
