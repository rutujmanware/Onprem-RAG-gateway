#!/usr/bin/env python3
"""Unit and Integration tests for Dynamic Similarity-Based Document Retrieval."""

import unittest
from pipeline.retriever import RedisChunkRetriever


class TestDynamicScoreRetrieval(unittest.TestCase):

    def setUp(self):
        self.retriever = RedisChunkRetriever()

    def test_filter_dynamic_chunks_absolute_floor(self):
        """Verify that candidates below the absolute similarity floor are rejected."""
        mock_chunks = [
            {"chunk_key": "c1", "similarity": 0.42},
            {"chunk_key": "c2", "similarity": 0.38},
            {"chunk_key": "c3", "similarity": 0.25},
        ]
        # Floor = 0.48 -> All should be rejected
        selected = RedisChunkRetriever.filter_dynamic_chunks(mock_chunks, min_similarity=0.48)
        self.assertEqual(len(selected), 0, "Expected 0 chunks when all are below min_similarity floor")

    def test_filter_dynamic_chunks_relative_margin(self):
        """Verify that chunks with large score gaps from the top candidate are dropped."""
        mock_chunks = [
            {"chunk_key": "c1", "similarity": 0.85},
            {"chunk_key": "c2", "similarity": 0.82}, # Drop 0.03 -> Keep
            {"chunk_key": "c3", "similarity": 0.79}, # Drop 0.06 -> Keep
            {"chunk_key": "c4", "similarity": 0.71}, # Drop 0.14 (> 0.10 margin) -> Drop
            {"chunk_key": "c5", "similarity": 0.70},
        ]
        selected = RedisChunkRetriever.filter_dynamic_chunks(
            mock_chunks,
            min_similarity=0.48,
            score_margin=0.10,
            max_knee_drop=0.10,
        )
        keys = [c["chunk_key"] for c in selected]
        self.assertEqual(keys, ["c1", "c2", "c3"], f"Expected ['c1', 'c2', 'c3'] but got {keys}")

    def test_filter_dynamic_chunks_knee_elbow_drop(self):
        """Verify that sudden steep drops between consecutive ranks trigger early truncation."""
        mock_chunks = [
            {"chunk_key": "c1", "similarity": 0.80},
            {"chunk_key": "c2", "similarity": 0.78}, # Drop 0.02 -> Keep
            {"chunk_key": "c3", "similarity": 0.70}, # Consecutive drop 0.08 (> 0.06) -> Elbow stop!
            {"chunk_key": "c4", "similarity": 0.69},
        ]
        selected = RedisChunkRetriever.filter_dynamic_chunks(
            mock_chunks,
            min_similarity=0.48,
            score_margin=0.15,
            max_knee_drop=0.06,
        )
        keys = [c["chunk_key"] for c in selected]
        self.assertEqual(keys, ["c1", "c2"], f"Expected elbow truncation at c2, got {keys}")

    def test_filter_dynamic_chunks_max_k_clamp(self):
        """Verify that max_k upper bound limit is enforced even if more chunks qualify."""
        mock_chunks = [
            {"chunk_key": f"c{i}", "similarity": 0.80 - (i * 0.01)}
            for i in range(10)
        ]
        selected = RedisChunkRetriever.filter_dynamic_chunks(
            mock_chunks,
            min_similarity=0.48,
            score_margin=0.15,
            max_knee_drop=0.06,
            max_k=3,
        )
        self.assertEqual(len(selected), 3, "Expected max_k clamp of 3")

    def test_filter_dynamic_chunks_adaptive_borderline_tightening(self):
        """Verify that borderline matches (< 0.52) tighten the margin to 0.05 to avoid drift."""
        mock_chunks = [
            {"chunk_key": "c1", "similarity": 0.50}, # Top match is borderline (< 0.52)
            {"chunk_key": "c2", "similarity": 0.48}, # Drop 0.02 -> Keep
            {"chunk_key": "c3", "similarity": 0.44}, # Drop 0.06 (> tightened 0.05 margin) -> Drop
        ]
        selected = RedisChunkRetriever.filter_dynamic_chunks(
            mock_chunks,
            min_similarity=0.40,
            score_margin=0.10,
            max_knee_drop=0.06,
        )
        keys = [c["chunk_key"] for c in selected]
        self.assertEqual(keys, ["c1", "c2"])

    def test_live_out_of_domain_rejection(self):
        """Live test: completely out-of-domain query returns 0 chunks in dynamic mode."""
        chunks = self.retriever.search(
            "Who won the FIFA World Cup football tournament in 2022?",
            dynamic=True,
        )
        self.assertEqual(
            len(chunks),
            0,
            f"Expected 0 chunks for out-of-domain query, got {len(chunks)}: {[c['similarity'] for c in chunks]}"
        )

    def test_live_philosophical_out_of_domain_rejection(self):
        """Live test: philosophical/abstract queries without grounded documents return 0 chunks."""
        for query in ["what is the meaning of meaning", "what is the meaning of life"]:
            chunks = self.retriever.search(query, dynamic=True)
            self.assertEqual(
                len(chunks),
                0,
                f"Expected 0 chunks for '{query}', got {len(chunks)}: {[c.get('similarity') for c in chunks]}"
            )

    def test_live_in_domain_dynamic_retrieval(self):
        """Live test: genuine in-domain query retrieves multiple relevant chunks with high similarity."""
        chunks = self.retriever.search(
            "What is vermiculite and what are its uses?",
            dynamic=True,
        )
        self.assertGreaterEqual(
            len(chunks),
            1,
            "Expected at least 1 chunk dynamically retrieved for vermiculite query"
        )
        # All returned chunks must strictly meet or exceed the floor threshold
        for c in chunks:
            self.assertGreaterEqual(
                c["similarity"],
                0.48,
                f"Chunk {c.get('title', 'chunk')} similarity {c['similarity']} was below 0.48 floor"
            )

    def test_response_excludes_internal_keys(self):
        """Verify chunk_key, handle_id, chunk_index, and vector are not returned in chunks."""
        chunks = self.retriever.search(
            "What is vermiculite and what are its uses?",
            dynamic=True,
        )
        self.assertGreaterEqual(len(chunks), 1)
        for c in chunks:
            self.assertNotIn("chunk_key", c, "chunk_key must not be returned in response")
            self.assertNotIn("handle_id", c, "handle_id must not be returned in response")
            self.assertNotIn("chunk_index", c, "chunk_index must not be returned in response")
            self.assertNotIn("vector", c, "raw vector embedding must not be returned in response")


if __name__ == "__main__":
    unittest.main()

