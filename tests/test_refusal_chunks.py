#!/usr/bin/env python3
"""Unit tests verifying that answers indicating no relevant documents were found do not serve document chunks/citations."""

import json
import unittest
from pipeline.sanitizer import is_no_relevant_docs_answer


class TestRefusalChunks(unittest.TestCase):

    def test_refusal_detection_canonical_phrases(self):
        """Test canonical refusal and insufficient data phrases."""
        refusal_examples = [
            "No specific institutional repository theses were found on this topic.",
            "no specific institutional repository theses were found",
            "No institutional repository theses were found on this topic.",
            "No relevant documents are present in the thesis.",
            "no relevant documents were found regarding the query",
            "No documents were found matching your query.",
            "The provided documents do not contain information to answer this question.",
            "Based on the provided documents, there is no information about quantum dots.",
            "Cannot answer based on the provided documents.",
            "Retrieval failed: Connection refused to Redis.",
            "Insufficient information in the provided context to answer.",
            "No documents directly cover this topic in the institutional repository.",
            "No relevant document was found in the thesis collection.",
            "No relevant theses were found on this topic.",
            "The retrieved documents do not contain information regarding this topic.",
            "There is no information regarding 2026 faculty appointments in the provided theses.",
            "No information is available on this topic in the retrieved thesis corpus.",
            "Unable to answer based on the retrieved context.",
            "The context does not contain enough information to determine the result.",
            "I do not have enough information to provide an answer.",
        ]
        for phrase in refusal_examples:
            self.assertTrue(
                is_no_relevant_docs_answer(phrase),
                f"Failed to identify refusal: {phrase!r}"
            )

    def test_factual_answers_not_flagged(self):
        """Test that factual grounded answers are NOT flagged as refusals."""
        factual_examples = [
            "In Chapter 2, the author describes the fabrication of GaN nanowires using chemical vapor deposition.",
            "This thesis presents a 12-bit SAR ADC implemented in a 65nm CMOS process.",
            "The experimental results demonstrate a 25% improvement in thermal conductivity for copper-graphene composites.",
            "We analyzed 45 deep learning models trained on medical imaging datasets.",
            "According to the thesis by Dr. Sharma, the Rayleigh-Taylor instability occurs at the fluid boundary.",
            "The research documents a 15% increase in power conversion efficiency under standard AM1.5G illumination.",
            "The document establishes that topological insulators possess gapless edge or surface states.",
        ]
        for ans in factual_examples:
            self.assertFalse(
                is_no_relevant_docs_answer(ans),
                f"Factual answer wrongly flagged as refusal: {ans!r}"
            )

    def test_non_string_or_empty(self):
        """Test edge cases like None, empty string, non-strings."""
        self.assertFalse(is_no_relevant_docs_answer(""))
        self.assertFalse(is_no_relevant_docs_answer("   "))
        self.assertFalse(is_no_relevant_docs_answer(None))
        self.assertFalse(is_no_relevant_docs_answer(123))
        self.assertFalse(is_no_relevant_docs_answer(["some list"]))

    def test_chunk_clearing_logic(self):
        """Verify served_chunks logic: refusal clears chunks, factual preserves chunks."""
        mock_chunks = [
            {"doc_id": "thesis_001", "content": "Some thesis text", "score": 0.85},
            {"doc_id": "thesis_002", "content": "Another thesis text", "score": 0.72},
        ]
        refusal_ans = "No specific institutional repository theses were found on this topic."
        served_when_refusal = [] if is_no_relevant_docs_answer(refusal_ans) else mock_chunks
        self.assertEqual(served_when_refusal, [])

        factual_ans = "The thesis outlines a novel method for synthesis of ZnO nanorods."
        served_when_factual = [] if is_no_relevant_docs_answer(factual_ans) else mock_chunks
        self.assertEqual(len(served_when_factual), 2)
        self.assertEqual(served_when_factual[0]["doc_id"], "thesis_001")

    def test_streaming_event_simulation(self):
        """Simulate streaming gateway events when model returns a refusal."""
        initial_chunks = [
            {"doc_id": "thesis_abc", "content": "Unrelated topic", "score": 0.3}
        ]
        events = [
            {"type": "route", "route": "easy", "target_tier": "cpu_slm"},
            {"type": "chunks", "chunks": initial_chunks},
            {"type": "token", "delta": "No specific institutional "},
            {"type": "token", "delta": "repository theses were found on this topic."},
        ]

        # Emulate gateway backend collector
        collected_tokens = []
        collected_chunks = initial_chunks
        cleared_chunks_emitted = False

        for ev in events:
            if ev["type"] == "token":
                collected_tokens.append(ev["delta"])
                full_so_far = "".join(collected_tokens)
                if is_no_relevant_docs_answer(full_so_far):
                    cleared_chunks_emitted = True
                    collected_chunks = []

        self.assertTrue(cleared_chunks_emitted)
        self.assertEqual(collected_chunks, [])

    def test_repeated_thesis_titles_deduplication(self):
        """Verify that multiple chunks from the same thesis title are deduplicated using a set."""
        from pipeline.retriever import sanitize_chunks

        chunks_with_duplicates = [
            {"title": "Fabrication of GaN Nanowires", "content": "Paragraph 1", "similarity": 0.88},
            {"title": "Fabrication of GaN Nanowires", "content": "Paragraph 2", "similarity": 0.85},
            {"title": "Study of Zinc Oxide Thin Films", "content": "Introduction", "similarity": 0.81},
            {"title": "Fabrication of GaN Nanowires", "content": "Conclusion", "similarity": 0.79},
            {"title": "Study of Zinc Oxide Thin Films", "content": "Section 3", "similarity": 0.75},
            {"title": "Novel Carbon Nanotube Composites", "content": "Abstract", "similarity": 0.70},
        ]

        deduped = sanitize_chunks(chunks_with_duplicates)
        # Should contain exactly 3 unique titles: GaN Nanowires, Zinc Oxide Thin Films, Carbon Nanotube Composites
        self.assertEqual(len(deduped), 3)
        titles = [c["title"] for c in deduped]
        self.assertEqual(titles, [
            "Fabrication of GaN Nanowires",
            "Study of Zinc Oxide Thin Films",
            "Novel Carbon Nanotube Composites",
        ])


if __name__ == "__main__":
    unittest.main()
